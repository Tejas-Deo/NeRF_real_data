import math
import trimesh
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

import raymarching
from .utils import custom_meshgrid
import matplotlib.pyplot as plt

def sample_pdf(bins, weights, n_samples, det=False):
    # This implementation is from NeRF
    # bins: [B, T], old_z_vals
    # weights: [B, T - 1], bin weights.
    # return: [B, n_samples], new_z_vals

    # Get pdf
    weights = weights + 1e-5  # prevent nans
    pdf = weights / torch.sum(weights, -1, keepdim=True)
    cdf = torch.cumsum(pdf, -1)
    cdf = torch.cat([torch.zeros_like(cdf[..., :1]), cdf], -1)
    # Take uniform samples
    if det:
        u = torch.linspace(0. + 0.5 / n_samples, 1. - 0.5 / n_samples, steps=n_samples).to(weights.device)
        u = u.expand(list(cdf.shape[:-1]) + [n_samples])
    else:
        u = torch.rand(list(cdf.shape[:-1]) + [n_samples]).to(weights.device)

    # Invert CDF
    u = u.contiguous()
    inds = torch.searchsorted(cdf, u, right=True)
    below = torch.max(torch.zeros_like(inds - 1), inds - 1)
    above = torch.min((cdf.shape[-1] - 1) * torch.ones_like(inds), inds)
    inds_g = torch.stack([below, above], -1)  # (B, n_samples, 2)

    matched_shape = [inds_g.shape[0], inds_g.shape[1], cdf.shape[-1]]
    cdf_g = torch.gather(cdf.unsqueeze(1).expand(matched_shape), 2, inds_g)
    bins_g = torch.gather(bins.unsqueeze(1).expand(matched_shape), 2, inds_g)

    denom = (cdf_g[..., 1] - cdf_g[..., 0])
    denom = torch.where(denom < 1e-5, torch.ones_like(denom), denom)
    t = (u - cdf_g[..., 0]) / denom
    samples = bins_g[..., 0] + t * (bins_g[..., 1] - bins_g[..., 0])

    return samples


def plot_pointcloud(pc, color=None):
    # pc: [N, 3]
    # color: [N, 3/4]
    print('[visualize points]', pc.shape, pc.dtype, pc.min(0), pc.max(0))
    pc = trimesh.PointCloud(pc, color)
    # axis
    axes = trimesh.creation.axis(axis_length=4)
    # sphere
    sphere = trimesh.creation.icosphere(radius=1)
    trimesh.Scene([pc, axes, sphere]).show()


class NeRFRenderer(nn.Module):
    def __init__(self,
                 bound=1,
                 cuda_ray=False,
                 density_scale=1, # scale up deltas (or sigmas), to make the density grid more sharp. larger value than 1 usually improves performance.
                #  min_near=0.2,
                #  max_far=100,
                 density_thresh=0.01,
                 bg_radius=-1,
                 ):
        super().__init__()

        self.bound = bound
        self.cascade = 1 + math.ceil(math.log2(bound))
        self.grid_size = 128
        self.density_scale = density_scale
        #self.min_near = 0.2
        # self.max_far = max_far
        self.density_thresh = density_thresh
        self.bg_radius = bg_radius # radius of the background sphere.

        # prepare aabb with a 6D tensor (xmin, ymin, zmin, xmax, ymax, zmax)
        # NOTE: aabb (can be rectangular) is only used to generate points, we still rely on bound (always cubic) to calculate density grid and hashing.
        aabb_train = torch.FloatTensor([-bound, -bound, -bound, bound, bound, bound])
        aabb_infer = aabb_train.clone()
        self.register_buffer('aabb_train', aabb_train)
        self.register_buffer('aabb_infer', aabb_infer)

        # extra state for cuda raymarching
        self.cuda_ray = cuda_ray
        if cuda_ray:
            # density grid
            density_grid = torch.zeros([self.cascade, self.grid_size ** 3]) # [CAS, H * H * H]
            density_bitfield = torch.zeros(self.cascade * self.grid_size ** 3 // 8, dtype=torch.uint8) # [CAS * H * H * H // 8]
            self.register_buffer('density_grid', density_grid)
            self.register_buffer('density_bitfield', density_bitfield)
            self.mean_density = 0
            self.iter_density = 0
            # step counter
            step_counter = torch.zeros(16, 2, dtype=torch.int32) # 16 is hardcoded for averaging...
            self.register_buffer('step_counter', step_counter)
            self.mean_count = 0
            self.local_step = 0
    
    def forward(self, x, d):
        raise NotImplementedError()

    # separated density and color query (can accelerate non-cuda-ray mode.)
    def density(self, x):
        raise NotImplementedError()

    def color(self, x, d, mask=None, **kwargs):
        raise NotImplementedError()

    def reset_extra_state(self):
        if not self.cuda_ray:
            return 
        # density grid
        self.density_grid.zero_()
        self.mean_density = 0
        self.iter_density = 0
        # step counter
        self.step_counter.zero_()
        self.mean_count = 0
        self.local_step = 0

    def run(self, rays_o, rays_d, num_steps=128, upsample_steps=128, 
            bg_color=None, perturb=False, datatype='rgb',
            max_far=5, min_near=.2, **kwargs):
        # rays_o, rays_d: [B, N, 3], assumes B == 1
        # bg_color: [3] in range [0, 1]
        # return: image: [B, N, 3], depth: [B, N]


        prefix = rays_o.shape[:-1]
        rays_o = rays_o.contiguous().view(-1, 3)
        rays_d = rays_d.contiguous().view(-1, 3)

        N = rays_o.shape[0] # N = B * N, in fact
        device = rays_o.device

        # choose aabb
        aabb = self.aabb_train if self.training else self.aabb_infer

        # sample steps
        nears, fars = raymarching.near_far_from_aabb(rays_o, rays_d, aabb, 0)
        #print("nears and fars")
        #print(torch.max(fars))
        #print(torch.min(fars))
        #print(torch.max(nears))
        #print(torch.min(nears))
        fars = torch.max(torch.min(fars, torch.tensor(max_far).to(device=device)), torch.tensor(min_near).to(device=device))
        nears = torch.min(torch.max(nears, torch.tensor(min_near).to(device=device)),torch.tensor(max_far).to(device=device))
        #if datatype == 'rgb' or datatype == 'depth':
        #    print("max far and near")
        #    print(torch.tensor(max_far))
        #    print(torch.tensor(min_near))
        #    print("chosen far and near")
        #    print(torch.max(fars))
        #    print(torch.min(fars))
        #    print(torch.max(nears))
        #    print(torch.min(nears))
        
        nears.unsqueeze_(-1)
        fars.unsqueeze_(-1)
        #print(" ")

        #print(f'nears = {nears.min().item()} ~ {nears.max().item()}, fars = {fars.min().item()} ~ {fars.max().item()}')

        z_vals = torch.linspace(0.0, 1.0, num_steps, device=device).unsqueeze(0) # [1, T]
        z_vals = z_vals.expand((N, num_steps)) # [N, T]
        z_vals = nears + (fars - nears) * z_vals # [N, T], in [nears, fars]

        #if datatype == 'rgb' or datatype == 'depth':
        #    print("Z VALS")
        #    print(nears)
        #    print(fars)
        #    print(torch.all(fars>nears))
        #    print(torch.linspace(0.0, 1.0, num_steps, device=device).unsqueeze(0))
        #    print(z_vals)
        #print(torch.max(z_vals))
        #print(torch.min(z_vals))
        #print(" ")

        '''
        if datatype == 'rgb':
            fig = plt.figure()
            ax = fig.add_subplot(projection='3d')
            print(rays_o.shape)
            print(rays_d.shape)
            print(z_vals.shape)
            rays_o = rays_o.reshape(-1,1,3).repeat(1,z_vals.shape[-1],1) 
            rays_d = rays_d.reshape(-1,1,3).repeat(1,z_vals.shape[-1],1)
            points = rays_o + z_vals.reshape(z_vals.shape[0],z_vals.shape[1], 1)*rays_d
            print(points.shape)
            points = points.reshape(-1,3).detach().cpu().numpy()
            inds = np.random.choice(np.arange(points.shape[0]), size=100000, replace=False)
            points = points[inds,:]
            print(points.shape)
            sc = ax.scatter(points[:,0],points[:,1],points[:,2])
            ax.set_xlabel('X Label')
            ax.set_ylabel('Y Label')
            ax.set_zlabel('Z Label')
            plt.show()
            #points[:,-1] = z_vals
            stop
        '''

        # perturb z_vals
        sample_dist = (fars - nears) / num_steps
        #if datatype == 'rgb' or datatype == 'depth':
        #    print("sample dist")
        #    print(sample_dist)
        #    print(torch.any(torch.isnan(sample_dist)))
        if perturb:
            z_vals = z_vals + (torch.rand(z_vals.shape, device=device) - 0.5) * sample_dist
            #z_vals = z_vals.clamp(nears, fars) # avoid out of bounds xyzs.

        # generate xyzs
        xyzs = rays_o.unsqueeze(-2) + rays_d.unsqueeze(-2) * z_vals.unsqueeze(-1) # [N, 1, 3] * [N, T, 1] -> [N, T, 3]
        xyzs = torch.min(torch.max(xyzs, aabb[:3]), aabb[3:]) # a manual clip.
        
        #if datatype == 'rgb' or datatype == 'depth':
        #    print("xyzs")
        #    print(torch.any(torch.isnan(xyzs)))

        #plot_pointcloud(xyzs.reshape(-1, 3).detach().cpu().numpy())

        # query SDF and RGB
        density_outputs = self.density(xyzs.reshape(-1, 3))
        
        #if datatype == 'rgb' or datatype == 'depth':
        #    print("density")
        #    print(density_outputs)

        #sigmas = density_outputs['sigma'].view(N, num_steps) # [N, T]
        for k, v in density_outputs.items():
            density_outputs[k] = v.view(N, num_steps, -1)

        # upsample z_vals (nerf-like)
        if upsample_steps > 0:
            with torch.no_grad():

                deltas = z_vals[..., 1:] - z_vals[..., :-1] # [N, T-1]
                deltas = torch.cat([deltas, sample_dist * torch.ones_like(deltas[..., :1])], dim=-1)

                alphas = 1 - torch.exp(-deltas * self.density_scale * density_outputs['sigma'].squeeze(-1)) # [N, T]
                alphas_shifted = torch.cat([torch.ones_like(alphas[..., :1]), 1 - alphas + 1e-15], dim=-1) # [N, T+1]
                weights = alphas * torch.cumprod(alphas_shifted, dim=-1)[..., :-1] # [N, T]

                # sample new z_vals
                z_vals_mid = (z_vals[..., :-1] + 0.5 * deltas[..., :-1]) # [N, T-1]
                new_z_vals = sample_pdf(z_vals_mid, weights[:, 1:-1], upsample_steps, det=not self.training).detach() # [N, t]

                new_xyzs = rays_o.unsqueeze(-2) + rays_d.unsqueeze(-2) * new_z_vals.unsqueeze(-1) # [N, 1, 3] * [N, t, 1] -> [N, t, 3]
                new_xyzs = torch.min(torch.max(new_xyzs, aabb[:3]), aabb[3:]) # a manual clip.

            # only forward new points to save computation
            new_density_outputs = self.density(new_xyzs.reshape(-1, 3))
            #new_sigmas = new_density_outputs['sigma'].view(N, upsample_steps) # [N, t]
            for k, v in new_density_outputs.items():
                new_density_outputs[k] = v.view(N, upsample_steps, -1)

            # re-order
            z_vals = torch.cat([z_vals, new_z_vals], dim=1) # [N, T+t]
            z_vals, z_index = torch.sort(z_vals, dim=1)

            xyzs = torch.cat([xyzs, new_xyzs], dim=1) # [N, T+t, 3]
            xyzs = torch.gather(xyzs, dim=1, index=z_index.unsqueeze(-1).expand_as(xyzs))

            for k in density_outputs:
                tmp_output = torch.cat([density_outputs[k], new_density_outputs[k]], dim=1)
                density_outputs[k] = torch.gather(tmp_output, dim=1, index=z_index.unsqueeze(-1).expand_as(tmp_output))

        deltas = z_vals[..., 1:] - z_vals[..., :-1] # [N, T+t-1]
        deltas = torch.cat([deltas, sample_dist * torch.ones_like(deltas[..., :1])], dim=-1)
        alphas = 1 - torch.exp(-deltas * self.density_scale * density_outputs['sigma'].squeeze(-1)) # [N, T+t]
        alphas_shifted = torch.cat([torch.ones_like(alphas[..., :1]), 1 - alphas + 1e-15], dim=-1) # [N, T+t+1]
        weights = alphas * torch.cumprod(alphas_shifted, dim=-1)[..., :-1] # [N, T+t]

        #if datatype == 'rgb' or datatype == 'depth':
        #    print("deltas")
        #    print(torch.all(torch.isfinite(deltas)))
        #    print(torch.any(deltas<0))
        #    print("alphas")
        #    print(torch.all(torch.isfinite(alphas)))
        #    print("weights")
        #    print(torch.all(torch.isfinite(weights)))

        dirs = rays_d.view(-1, 1, 3).expand_as(xyzs)
        for k, v in density_outputs.items():
            density_outputs[k] = v.view(-1, v.shape[-1])

        mask = weights > 1e-4 # hard coded
        rgbs = self.color(xyzs.reshape(-1, 3), dirs.reshape(-1, 3), mask=mask.reshape(-1), **density_outputs)
        rgbs = rgbs.view(N, -1, 3) # [N, T+t, 3]

        #print(xyzs.shape, 'valid_rgb:', mask.sum().item())

        # calculate weight_sum (mask)
        weights_sum = weights.sum(dim=-1) # [N]
        
        # calculate depth 
        #ori_z_vals = ((z_vals - nears) / (fars - nears)).clamp(0, 1)
        #depth = torch.sum(weights * ori_z_vals, dim=-1)
        
        if max_far is not np.inf or min_near is not np.inf:
            #print("ENTERED BOUNDED RAY MATCH")
            #print("weights")
            #print(torch.all(torch.isfinite(weights)))
            #print("z vals")
            #print(torch.all(torch.isfinite(z_vals)))
            #print("max far")
            #print(max_far)
            #print("depth")
            #print(torch.all(torch.isfinite(torch.sum(weights * z_vals, dim=-1))))
            depth = torch.sum(weights * z_vals, dim=-1)
            depth = depth + (1-weights_sum)*max_far
            d_var = torch.sum(torch.square(z_vals - depth.reshape(-1,1)), dim=-1)/(z_vals.shape[-1]-1)
        else:
            #print("ENTERED UNBOUNDED RAY MATH")
            ori_z_vals = ((z_vals - nears) / (fars - nears)).clamp(0, 1)
            depth = torch.sum(weights * ori_z_vals, dim=-1)
            d_var = torch.sum(torch.square(ori_z_vals - depth.reshape(-1,1)), dim=-1)/(ori_z_vals.shape[-1]-1)
        
        # calculate color
        image = torch.sum(weights.unsqueeze(-1) * rgbs, dim=-2) # [N, 3], in [0, 1]

        # mix background color
        if self.bg_radius > 0:
            # use the bg model to calculate bg_color
            sph = raymarching.sph_from_ray(rays_o, rays_d, self.bg_radius) # [N, 2] in [-1, 1]
            bg_color = self.background(sph, rays_d.reshape(-1, 3)) # [N, 3]
        elif bg_color is None:
            bg_color = 1
            
        image = image + (1 - weights_sum).unsqueeze(-1) * bg_color

        image = image.view(*prefix, 3)
        depth = depth.view(*prefix)

        # tmp: reg loss in mip-nerf 360
        # z_vals_shifted = torch.cat([z_vals[..., 1:], sample_dist * torch.ones_like(z_vals[..., :1])], dim=-1)
        # mid_zs = (z_vals + z_vals_shifted) / 2 # [N, T]
        # loss_dist = (torch.abs(mid_zs.unsqueeze(1) - mid_zs.unsqueeze(2)) * (weights.unsqueeze(1) * weights.unsqueeze(2))).sum() + 1/3 * ((z_vals_shifted - z_vals_shifted) * (weights ** 2)).sum()
        if datatype == 'rgb' or datatype == 'depth':
            if torch.any(torch.isnan(depth)):
                print('z vals')
                print(z_vals)
                print('depth')
                print(depth)
                print('image')
                print(image)
            
                fig = plt.figure()
                ax = fig.add_subplot(projection='3d')
                print(rays_o.shape)
                print(rays_d.shape)
                print(z_vals.shape)
                rays_o = rays_o.reshape(-1,1,3).repeat(1,z_vals.shape[-1],1) 
                rays_d = rays_d.reshape(-1,1,3).repeat(1,z_vals.shape[-1],1)
                points = rays_o + z_vals.reshape(z_vals.shape[0],z_vals.shape[1], 1)*rays_d
                print(points.shape)
                points = points.reshape(-1,3).detach().cpu().numpy()
                inds = np.random.choice(np.arange(points.shape[0]), size=100000, replace=False)
                points = points[inds,:]
                print(points.shape)
                sc = ax.scatter(points[:,0],points[:,1],points[:,2])
                ax.set_xlabel('X Label')
                ax.set_ylabel('Y Label')
                ax.set_zlabel('Z Label')
                plt.show()
                stop
        
        #print(depth)
        #print(image)
       
        return {
            'depth': depth,
            'depth_var': d_var,
            'image': image,
            'weights_sum': weights_sum,
        }

        """
        #print("HELLO NURSE!")
        #print(min_near)
        #print(max_far)
        prefix = rays_o.shape[:-1]
        rays_o = rays_o.contiguous().view(-1, 3)
        rays_d = rays_d.contiguous().view(-1, 3)

        N = rays_o.shape[0] # N = B * N, in fact
        device = rays_o.device

        # choose aabb
        aabb = self.aabb_train if self.training else self.aabb_infer

        # sample steps
        
        if min_near is np.inf or max_far is np.inf:
            print("ENTERED UNBOUNDED RAY MARCHING")
            nears, fars = raymarching.near_far_from_aabb(rays_o, rays_d, aabb, 0.2)
        else:
            print("ENTERED BOUNDED RAY MARCHING")
            nears, fars = raymarching.near_far_from_aabb(rays_o, rays_d, aabb, min_near)
        nears.unsqueeze_(-1)
        fars.unsqueeze_(-1)
        # print(min_near)
        # print("AABB")
        # print(aabb)
        # print("min x y z, max x y z")
        # print("VALS")
        # print(fars)
        # print(" ")
        # print(nears)
        # print("origin")
        # print(rays_o)
        # print("direction")
        
        #print(self.max_far)

        #print(f'nears = {nears.min().item()} ~ {nears.max().item()}, fars = {fars.min().item()} ~ {fars.max().item()}')
        #print("BEFORE TRUNCATING!")
        #print("NEARS")
        #print(nears)
        #print(torch.max(nears))
        #print(torch.min(nears))
        #print("FARS")
        #print(fars)
        #print(torch.max(fars))
        #print(torch.min(fars))
        
        #if datatype != "viewer":
        print("before clip")
        print(torch.max(fars))
        print(torch.min(fars))
        print(torch.max(nears))
        print(torch.min(nears))
        #print(float(torch.min(nears).item()))
        #print(float(np.inf))
        
        if min_near is not np.inf and max_far is not np.inf:
            fars = torch.min(fars, torch.tensor(max_far).to(device=device))
        
        # for some reason, raymarching returns large infinite value for nears when ray is infinite
        # therefore we use absolute on negative infinite near plane value to signify leaving rays unchanged
        if min_near is not np.inf and max_far is not np.inf:
            nears = torch.min(nears, torch.tensor(np.abs(min_near)).to(device=device))
        print("after clip")
        print(torch.max(fars))
        print(torch.min(fars))
        print(torch.max(nears))
        print(torch.min(nears))
        #print("start")
        #print(torch.min(fars))
        #print(torch.max(fars))
        #fars = torch.min(fars, torch.tensor(max_far).to(device=device))
        #print(torch.min(nears))
        #print(torch.max(nears))
        #nears = torch.min(nears, torch.tensor(min_near).to(device=device))
        #print("end")
        print(" ")
        #print(ray_far)
        # print(aabb)
        # print(aabb.requires_grad)
        # print(torch.max(fars))
        # print(torch.min(nears))
        # stop

        z_vals = torch.linspace(0.0, 1.0, num_steps, device=device).unsqueeze(0) # [1, T]
        z_vals = z_vals.expand((N, num_steps)) # [N, T]
        z_vals = nears + (fars - nears) * z_vals # [N, T], in [nears, fars]
        
        print("z vals")
        print(torch.max(z_vals))
        print(torch.min(z_vals))
        print(" ")

        
        #print("CHECKING RENDER")
        #print("EVERYTHING BEFORE 181 is okay!")
        # perturb z_vals
        sample_dist = (fars - nears) / num_steps
        
        print("sample_dist")
        print(torch.max(sample_dist))
        print(torch.min(sample_dist))
        print(" ")

        #print("SAMPLE DISTRIBUTION")
        #print(sample_dist)
        #print(torch.max(sample_dist))
        #print(torch.min(sample_dist))
        #print("NEARS")
        #print(nears)
        #print(torch.max(nears))
        #print("FARS")
        #print(fars)
        #print(torch.max(fars))
        if perturb:
        #    print("PERTURBING RESULTS")
            vals = (torch.rand(z_vals.shape, device=device) - 0.5) * sample_dist
        #    print(torch.any(torch.isnan(vals)))
        #    print(torch.any(torch.isinf(vals)))
            z_vals = z_vals + vals#(torch.rand(z_vals.shape, device=device) - 0.5) * sample_dist
            #z_vals = z_vals.clamp(nears, fars) # avoid out of bounds xyzs.
            

        # generate xyzs
        xyzs = rays_o.unsqueeze(-2) + rays_d.unsqueeze(-2) * z_vals.unsqueeze(-1) # [N, 1, 3] * [N, T, 1] -> [N, T, 3]
        #print("calculated xyzs")
        #print(xyzs)
        
        
        xyzs = torch.min(torch.max(xyzs, aabb[:3]), aabb[3:]) # a manual clip.

        print('xyzs')
        print(torch.any(torch.isnan(xyzs)))
        print(torch.any(torch.isinf(xyzs)))
        print(" ")

        points = xyzs.reshape(-1,3).detach().cpu().numpy()
        rays =  (rays_o + rays_d).reshape(-1,3).detach().cpu().numpy()
        origins = rays_o.reshape(-1,3).detach().cpu().numpy()
        
        '''
        if datatype != "viewer":
             boxsize = 2
             print("points")
             print(points.shape)
             print(origins.shape)
             print(origins)
             inds = np.random.choice(points.shape[0], 5000, replace=False)
             sampled_points = points[inds]

             fig = plt.figure()
             ax = fig.add_subplot(projection='3d')

             ax.plot([-boxsize, boxsize], [-boxsize,-boxsize],zs=[-boxsize,-boxsize])
             ax.plot([-boxsize, -boxsize], [-boxsize,boxsize],zs=[-boxsize,-boxsize])
             ax.plot([-boxsize, -boxsize], [-boxsize,-boxsize],zs=[-boxsize,boxsize])

             ax.plot([boxsize, boxsize], [-boxsize,boxsize],zs=[-boxsize,-boxsize])
             ax.plot([boxsize, boxsize], [-boxsize,-boxsize],zs=[-boxsize,boxsize])

             ax.plot([boxsize, -boxsize], [boxsize,boxsize],zs=[-boxsize,-boxsize])
             ax.plot([boxsize, boxsize], [boxsize,boxsize],zs=[-boxsize,boxsize])
            
             ax.plot([-boxsize, -boxsize], [boxsize,boxsize],zs=[-boxsize,boxsize])

             ax.plot([-boxsize, boxsize], [-boxsize,-boxsize],zs=[boxsize,boxsize])
             ax.plot([-boxsize, -boxsize], [-boxsize,boxsize],zs=[boxsize,boxsize])
             ax.plot([boxsize, boxsize], [-boxsize,boxsize],zs=[boxsize,boxsize])
             ax.plot([boxsize, -boxsize], [boxsize,boxsize],zs=[boxsize,boxsize])


             ## draw sphere
             #r = 0.125
             #u, v = np.mgrid[0:2*np.pi:20j, 0:np.pi:10j]
             #x = r*np.cos(u)*np.sin(v)
             #y = r*np.sin(u)*np.sin(v)
             #z = r*np.cos(v)
             #ax.plot_wireframe(x, y, z, color="r")
            
             #ax.scatter(sampled_points[:,0], sampled_points[:,1], sampled_points[:,2])
             #ax.scatter(origins[:,0], origins[:,1], origins[:,2], 'r',s=200)
             #ax.set_xlabel('X Label')
             #ax.set_ylabel('Y Label')
        
        #if datatype != "viewer":
        #     boxsize = 2
        #     print("points")
        #     print(points.shape)
        #     print(origins.shape)
        #     print(origins)
        #     inds = np.random.choice(points.shape[0], 5000, replace=False)
        #     sampled_points = points[inds]

        #     fig = plt.figure()
        #     ax = fig.add_subplot(projection='3d')

        #     ax.plot([-boxsize, boxsize], [-boxsize,-boxsize],zs=[-boxsize,-boxsize])
        #     ax.plot([-boxsize, -boxsize], [-boxsize,boxsize],zs=[-boxsize,-boxsize])
        #     ax.plot([-boxsize, -boxsize], [-boxsize,-boxsize],zs=[-boxsize,boxsize])

        #     ax.plot([boxsize, boxsize], [-boxsize,boxsize],zs=[-boxsize,-boxsize])
        #     ax.plot([boxsize, boxsize], [-boxsize,-boxsize],zs=[-boxsize,boxsize])

        #     ax.plot([boxsize, -boxsize], [boxsize,boxsize],zs=[-boxsize,-boxsize])
        #     ax.plot([boxsize, boxsize], [boxsize,boxsize],zs=[-boxsize,boxsize])
            
        #     ax.plot([-boxsize, -boxsize], [boxsize,boxsize],zs=[-boxsize,boxsize])

        #     ax.plot([-boxsize, boxsize], [-boxsize,-boxsize],zs=[boxsize,boxsize])
        #     ax.plot([-boxsize, -boxsize], [-boxsize,boxsize],zs=[boxsize,boxsize])
        #     ax.plot([boxsize, boxsize], [-boxsize,boxsize],zs=[boxsize,boxsize])
        #     ax.plot([boxsize, -boxsize], [boxsize,boxsize],zs=[boxsize,boxsize])


             # draw sphere
             r = 0.125
             u, v = np.mgrid[0:2*np.pi:20j, 0:np.pi:10j]
             x = r*np.cos(u)*np.sin(v)
             y = r*np.sin(u)*np.sin(v)
             z = r*np.cos(v)
             ax.plot_wireframe(x, y, z, color="r")
            
             ax.scatter(sampled_points[:,0], sampled_points[:,1], sampled_points[:,2])
             ax.scatter(origins[:,0], origins[:,1], origins[:,2], 'r',s=200)
             ax.set_xlabel('X Label')
             ax.set_ylabel('Y Label')
             ax.set_zlabel('Z Label')
             plt.show()
        #     stop
        #     ax.set_zlabel('Z Label')
        #     plt.show()
        #     stop
        #     print(points[inds].shape)
        
        # if datatype != "viewer":
        #     fig = plt.figure()
        #     ax = fig.add_subplot(projection='3d')
        #     ax.view_init(120, 30)
        #     ax.scatter(points[:,0], points[:,1], points[:,2])
        #     ax.scatter(rays[:,0], rays[:,1], rays[:,2])
        #     ax.scatter(origins[:,0], origins[:,1], origins[:,2])
        #     ax.set_xlabel('X Label')
        #     ax.set_ylabel('Y Label')
        #     ax.set_zlabel('Z Label')

        #     plt.savefig('foo.png')
        #     ax.view_init(60, 30)
        #     plt.savefig('fang.png')
        #     ax.view_init(20, 10)
        #     plt.savefig('tang.png')

        #     stop
        '''

        #print(xyzs.shape)
        #stop
        #print("clipped xyzs")
        #print(aabb)
        #print(xyzs)
        #print(xyzs.shape)
        #plot_pointcloud(xyzs.reshape(-1, 3).detach().cpu().numpy())

        # query SDF and RGB
        density_outputs = self.density(xyzs.reshape(-1, 3))

        #sigmas = density_outputs['sigma'].view(N, num_steps) # [N, T]
        for k, v in density_outputs.items():
            density_outputs[k] = v.view(N, num_steps, -1)

        # upsample z_vals (nerf-like)
        if upsample_steps > 0:
            print("ENTERED UPSAMPLING!")
            with torch.no_grad():
                deltas = z_vals[..., 1:] - z_vals[..., :-1] # [N, T-1]
                deltas = torch.cat([deltas, sample_dist * torch.ones_like(deltas[..., :1])], dim=-1)

                alphas = 1 - torch.exp(-deltas * self.density_scale * density_outputs['sigma'].squeeze(-1)) # [N, T]
                alphas_shifted = torch.cat([torch.ones_like(alphas[..., :1]), 1 - alphas + 1e-15], dim=-1) # [N, T+1]
                weights = alphas * torch.cumprod(alphas_shifted, dim=-1)[..., :-1] # [N, T]

                # sample new z_vals
                z_vals_mid = (z_vals[..., :-1] + 0.5 * deltas[..., :-1]) # [N, T-1]
                new_z_vals = sample_pdf(z_vals_mid, weights[:, 1:-1], upsample_steps, det=not self.training).detach() # [N, t]

                new_xyzs = rays_o.unsqueeze(-2) + rays_d.unsqueeze(-2) * new_z_vals.unsqueeze(-1) # [N, 1, 3] * [N, t, 1] -> [N, t, 3]
                new_xyzs = torch.min(torch.max(new_xyzs, aabb[:3]), aabb[3:]) # a manual clip.

            # only forward new points to save computation
            new_density_outputs = self.density(new_xyzs.reshape(-1, 3))
            #new_sigmas = new_density_outputs['sigma'].view(N, upsample_steps) # [N, t]
            for k, v in new_density_outputs.items():
                new_density_outputs[k] = v.view(N, upsample_steps, -1)

            # re-order
            z_vals = torch.cat([z_vals, new_z_vals], dim=1) # [N, T+t]
            z_vals, z_index = torch.sort(z_vals, dim=1)

            xyzs = torch.cat([xyzs, new_xyzs], dim=1) # [N, T+t, 3]
            xyzs = torch.gather(xyzs, dim=1, index=z_index.unsqueeze(-1).expand_as(xyzs))

            for k in density_outputs:
                tmp_output = torch.cat([density_outputs[k], new_density_outputs[k]], dim=1)
                density_outputs[k] = torch.gather(tmp_output, dim=1, index=z_index.unsqueeze(-1).expand_as(tmp_output))


        print("CHECKING RENDER")
        print("Z_VALS[..., 1:]")
        print(torch.any(torch.isnan(z_vals[..., 1:])))
        print(torch.any(torch.isinf(z_vals[..., 1:])))
        print("Z_VALS[..., :-1]")
        print(torch.any(torch.isnan(z_vals[..., :-1])))
        print(torch.any(torch.isinf(z_vals[..., :-1])))
        print("deltas")
        print(torch.any(torch.isnan(z_vals[..., 1:] - z_vals[..., :-1])))
        print(torch.any(torch.isinf(z_vals[..., 1:] - z_vals[..., :-1])))
        print("sample distribution")
        print(torch.any(torch.isnan(sample_dist)))
        print(torch.any(torch.isinf(sample_dist)))
        #print("CHECKING Z_VALUES")
        #print(z_vals)
        #print(z_vals[..., 1:])
        #print(z_vals[..., :-1])
        #print(z_vals[..., 1:] - z_vals[..., :-1])
        deltas = z_vals[..., 1:] - z_vals[..., :-1] # [N, T+t-1]
        deltas = torch.cat([deltas, sample_dist * torch.ones_like(deltas[..., :1])], dim=-1)
       
        #print("EXAMINGING DELTA VALUES")
        #print(torch.any((z_vals[..., 1:] - z_vals[..., :-1])<0))
        #print(torch.sum((z_vals[..., 1:] - z_vals[..., :-1])<0))
        #print(sample_dist * torch.ones_like(deltas[..., :1]))
        #print(sample_dist)
        #print(torch.sum(sample_dist<0))
        
        print("CHECKING RENDER")
        print("DELTAS")
        print(torch.any(torch.isnan(deltas)))
        print(torch.any(torch.isinf(deltas)))
        print("DENSITY SCALE")
        print(self.density_scale)
        print(self.density_scale)
        print("SIGMA")
        print(torch.any(torch.isnan(density_outputs['sigma'])))
        print(torch.any(torch.isinf(density_outputs['sigma'])))
        #print(torch.sum(deltas<0))
        #print(torch.exp(-deltas * self.density_scale * density_outputs['sigma'].squeeze(-1)))
        alphas = 1 - torch.exp(-deltas * self.density_scale * density_outputs['sigma'].squeeze(-1)) # [N, T+t]
        alphas_shifted = torch.cat([torch.ones_like(alphas[..., :1]), 1 - alphas + 1e-15], dim=-1) # [N, T+t+1]
        
        print("CHECKING RENDER")
        print("ALPHAS")
        print(torch.any(torch.isnan(alphas)))
        print(torch.any(torch.isinf(alphas)))
        print("INTEGRAL")
        print(torch.any(torch.isnan(torch.cumprod(alphas_shifted, dim=-1)[..., :-1])))
        print(torch.any(torch.isinf(torch.cumprod(alphas_shifted, dim=-1)[..., :-1])))


        weights = alphas * torch.cumprod(alphas_shifted, dim=-1)[..., :-1] # [N, T+t]

        dirs = rays_d.view(-1, 1, 3).expand_as(xyzs)
        for k, v in density_outputs.items():
            density_outputs[k] = v.view(-1, v.shape[-1])

        #print("EARLY WEIGHTS")
        #print(weights)
        mask = weights > 1e-4 # hard coded
        rgbs = self.color(xyzs.reshape(-1, 3), dirs.reshape(-1, 3), mask=mask.reshape(-1), **density_outputs)
        rgbs = rgbs.view(N, -1, 3) # [N, T+t, 3]

        #print(xyzs.shape, 'valid_rgb:', mask.sum().item())

        # calculate weight_sum (mask)
        weights_sum = weights.sum(dim=-1) # [N]
        
        # calculate depth 
        ori_z_vals = ((z_vals - nears) / (fars - nears)).clamp(0, 1)

        if max_far is not np.inf or min_near is not np.inf:
            #print("ENTERED BOUNDED RAY MATCH")
            depth = torch.sum(weights * z_vals, dim=-1)
            depth = depth + (1-weights_sum)*max_far
            d_var = torch.sum(weights*torch.square(depth.reshape(-1,1)-z_vals), dim=-1)
        else:
            #print("ENTERED UNBOUNDED RAY MATH")
            depth = torch.sum(weights * ori_z_vals, dim=-1)
            d_var = torch.sum(weights*torch.square(depth.reshape(-1,1)-ori_z_vals), dim=-1) + 1e-5
        
        # if datatype == 'rgb':
        #     depth = torch.sum(weights * ori_z_vals, dim=-1)
        #     d_var = torch.sum(weights*torch.square(depth.reshape(-1,1)-ori_z_vals), dim=-1) + 1e-5
        # else:
        #     depth = torch.sum(weights * z_vals, dim=-1)
        #     depth = depth + (1-weights_sum)*max_far
        #     #print("CHECK MAX DEPTH")
        #     #print(max_far)
        #     #print(depth)
        #     d_var = torch.sum(weights*torch.square(depth.reshape(-1,1)-z_vals), dim=-1) + 1e-5
        
        #print("SHAPES")
        #print(ori_z_vals.shape)
        #print(depth.shape)
        #print(d_var.shape)
        
        #print("HI")
        
        
        print("CHECKING RENDER")
        print("WEIGHTS")
        print(torch.any(torch.isnan(weights)))
        print(torch.any(torch.isinf(weights)))
        print("RGBS")
        print(torch.any(torch.isnan(rgbs)))
        print(torch.any(torch.isinf(rgbs)))

        # calculate color
        print("WEIGHTS")
        print(weights)
        image = torch.sum(weights.unsqueeze(-1) * rgbs, dim=-2) # [N, 3], in [0, 1]
        print("IMAGE")
        print(image)

        # mix background color
        if self.bg_radius > 0:
            # use the bg model to calculate bg_color
            sph = raymarching.sph_from_ray(rays_o, rays_d, self.bg_radius) # [N, 2] in [-1, 1]
            bg_color = self.background(sph, rays_d.reshape(-1, 3)) # [N, 3]
        elif bg_color is None:
            bg_color = 1
           
        
        #print("CHECKING RENDER")
        #print(torch.any(torch.isnan(image)))
        #print(torch.any(torch.isinf(image)))

        #print("IMAGE")
        #print(image) 

        image = image + (1 - weights_sum).unsqueeze(-1) * bg_color

        image_var = torch.sum(weights.unsqueeze(-1)*torch.square(image.unsqueeze(-2) - rgbs), dim=-2)
        # print((weights.unsqueeze(-1)*torch.square(image.unsqueeze(-2) - rgbs)).shape)

        image = image.view(*prefix, 3)
        image_var = image_var.view(*prefix, 3)
        depth = depth.view(*prefix)
        d_var = d_var.view(*prefix)
        
        # tmp: reg loss in mip-nerf 360
        # z_vals_shifted = torch.cat([z_vals[..., 1:], sample_dist * torch.ones_like(z_vals[..., :1])], dim=-1)
        # mid_zs = (z_vals + z_vals_shifted) / 2 # [N, T]
        # loss_dist = (torch.abs(mid_zs.unsqueeze(1) - mid_zs.unsqueeze(2)) * (weights.unsqueeze(1) * weights.unsqueeze(2))).sum() + 1/3 * ((z_vals_shifted - z_vals_shifted) * (weights ** 2)).sum()

        #print("OOOOOOOOIH")
        #print(depth)
        #print(depth.requires_grad)

        return {
            'depth': depth,
            'depth_var': d_var,
            'image': image,
            'image_var': image_var,
        }
    """

    def run_cuda(self, rays_o, rays_d, dt_gamma=0, 
                 bg_color=None, perturb=False, force_all_rays=False, 
                 max_steps=1024, datatype='rgb', max_far=5, min_near=.2, **kwargs):
        # rays_o, rays_d: [B, N, 3], assumes B == 1
        # return: image: [B, N, 3], depth: [B, N]

        prefix = rays_o.shape[:-1]
        rays_o = rays_o.contiguous().view(-1, 3)
        rays_d = rays_d.contiguous().view(-1, 3)

        N = rays_o.shape[0] # N = B * N, in fact
        device = rays_o.device

        # pre-calculate near far
        nears, fars = raymarching.near_far_from_aabb(rays_o, rays_d, self.aabb_train if self.training else self.aabb_infer, min_near)

        #print("OI MATE")
        #print(nears)
        #print(fars)
        #print("")

        # mix background color
        if self.bg_radius > 0:
            # use the bg model to calculate bg_color
            sph = raymarching.sph_from_ray(rays_o, rays_d, self.bg_radius) # [N, 2] in [-1, 1]
            bg_color = self.background(sph, rays_d) # [N, 3]
        elif bg_color is None:
            bg_color = 1

        if self.training:
            # setup counter
            counter = self.step_counter[self.local_step % 16]
            counter.zero_() # set to 0
            self.local_step += 1

            xyzs, dirs, deltas, rays = raymarching.march_rays_train(rays_o, rays_d, self.bound, self.density_bitfield, 
                                                                    self.cascade, self.grid_size, nears, ray_far, counter, 
                                                                    self.mean_count, perturb, 128, force_all_rays, dt_gamma, 
                                                                    max_steps)

            #plot_pointcloud(xyzs.reshape(-1, 3).detach().cpu().numpy())
            
            sigmas, rgbs = self(xyzs, dirs)
            # density_outputs = self.density(xyzs) # [M,], use a dict since it may include extra things, like geo_feat for rgb.
            # sigmas = density_outputs['sigma']
            # rgbs = self.color(xyzs, dirs, **density_outputs)
            sigmas = self.density_scale * sigmas

            #print(f'valid RGB query ratio: {mask.sum().item() / mask.shape[0]} (total = {mask.sum().item()})')


            # special case for CCNeRF's residual learning
            if len(sigmas.shape) == 2:
                K = sigmas.shape[0]
                depths = []
                images = []
                for k in range(K):
                    weights_sum, depth, image = raymarching.composite_rays_train(sigmas[k], rgbs[k], deltas, rays)
                    image = image + (1 - weights_sum).unsqueeze(-1) * bg_color
                    depth = torch.clamp(depth - nears, min=0) / (fars - nears)
                    images.append(image.view(*prefix, 3))
                    depths.append(depth.view(*prefix))
            
                depth = torch.stack(depths, axis=0) # [K, B, N]
                image = torch.stack(images, axis=0) # [K, B, N, 3]

            else:

                weights_sum, depth, image = raymarching.composite_rays_train(sigmas, rgbs, deltas, rays)
                image = image + (1 - weights_sum).unsqueeze(-1) * bg_color
                depth = torch.clamp(depth - nears, min=0) / (fars - nears)
                image = image.view(*prefix, 3)
                depth = depth.view(*prefix)

                #print("render training result")
                #print(image)
                #print(image.requires_grad)
                #print(depth)
                #print(depth.requires_grad)

        else:
           
            # allocate outputs 
            # if use autocast, must init as half so it won't be autocasted and lose reference.
            #dtype = torch.half if torch.is_autocast_enabled() else torch.float32
            # output should always be float32! only network inference uses half.
            dtype = torch.float32
            
            weights_sum = torch.zeros(N, dtype=dtype, device=device)
            depth = torch.zeros(N, dtype=dtype, device=device)
            image = torch.zeros(N, 3, dtype=dtype, device=device)
            
            n_alive = N
            rays_alive = torch.arange(n_alive, dtype=torch.int32, device=device) # [N]
            rays_t = nears.clone() # [N]

            step = 0
            
            while step < max_steps:

                # count alive rays 
                n_alive = rays_alive.shape[0]
                
                # exit loop
                if n_alive <= 0:
                    break

                # decide compact_steps
                n_step = max(min(N // n_alive, 8), 1)

                xyzs, dirs, deltas = raymarching.march_rays(n_alive, n_step, rays_alive, rays_t, 
                                                            rays_o, rays_d, self.bound, self.density_bitfield, 
                                                            self.cascade, self.grid_size, nears, fars, 128, perturb, 
                                                            dt_gamma, max_steps)

                sigmas, rgbs = self(xyzs, dirs)
                # density_outputs = self.density(xyzs) # [M,], use a dict since it may include extra things, like geo_feat for rgb.
                # sigmas = density_outputs['sigma']
                # rgbs = self.color(xyzs, dirs, **density_outputs)
                sigmas = self.density_scale * sigmas

                raymarching.composite_rays(n_alive, n_step, rays_alive, rays_t, sigmas, rgbs, deltas, weights_sum, depth, image)

                rays_alive = rays_alive[rays_alive >= 0]

                #print(f'step = {step}, n_step = {n_step}, n_alive = {n_alive}, xyzs: {xyzs.shape}')

                step += n_step

            image = image + (1 - weights_sum).unsqueeze(-1) * bg_color
            depth = torch.clamp(depth - nears, min=0) / (fars - nears)
            image = image.view(*prefix, 3)
            depth = depth.view(*prefix)


        #print("AH HOY THERE")
        #print(fars)
        #print(nears)
        #stop
        return {
            'depth': depth,
            'image': image,

        }

    @torch.no_grad()
    def mark_untrained_grid(self, poses_list, intrinsics_list, S=64):
        # poses: [B, 4, 4]
        # intrinsic: [3, 3]

        if not self.cuda_ray:
            return
        
        X = torch.arange(self.grid_size, dtype=torch.int32, device=self.density_bitfield.device).split(S)
        Y = torch.arange(self.grid_size, dtype=torch.int32, device=self.density_bitfield.device).split(S)
        Z = torch.arange(self.grid_size, dtype=torch.int32, device=self.density_bitfield.device).split(S)
        
        count = torch.zeros_like(self.density_grid)
        
        # 6 - level loop, ooops!
        #TODO: ensure that this works for touch data!!! unclear how far the cameras look.
        #      highly likely that we will need to enforce a boundary to prevent points outside
        #      the sensor from being considered inside the camera view!
        for i in range(len(poses_list)):
            if isinstance(poses_list[i], np.ndarray):
                poses = torch.from_numpy(poses_list[i])
            else:
                poses = poses_list[i]

            B = poses.shape[0]
            #print(intrinsics_list[i])
            #print(intrinsics_list)
            fx, fy, cx, cy = intrinsics_list[i]
        
            poses = poses.to(count.device)

            # 5-level loop, forgive me...
            for xs in X:
                for ys in Y:
                    for zs in Z:
                    
                        # construct points
                        xx, yy, zz = custom_meshgrid(xs, ys, zs)
                        coords = torch.cat([xx.reshape(-1, 1), yy.reshape(-1, 1), zz.reshape(-1, 1)], dim=-1) # [N, 3], in [0, 128)
                        indices = raymarching.morton3D(coords).long() # [N]
                        world_xyzs = (2 * coords.float() / (self.grid_size - 1) - 1).unsqueeze(0) # [1, N, 3] in [-1, 1]

                        # cascading
                        for cas in range(self.cascade):
                            bound = min(2 ** cas, self.bound)
                            half_grid_size = bound / self.grid_size
                            # scale to current cascade's resolution
                            cas_world_xyzs = world_xyzs * (bound - half_grid_size)

                            # split batch to avoid OOM
                            head = 0
                            while head < B:
                                tail = min(head + S, B)

                                # world2cam transform (poses is c2w, so we need to transpose it. Another transpose is needed for batched matmul, so the final form is without transpose.)
                                cam_xyzs = cas_world_xyzs - poses[head:tail, :3, 3].unsqueeze(1)
                                cam_xyzs = cam_xyzs @ poses[head:tail, :3, :3] # [S, N, 3]
                            
                                # query if point is covered by any camera
                                mask_z = cam_xyzs[:, :, 2] > 0 # [S, N]
                                mask_x = torch.abs(cam_xyzs[:, :, 0]) < cx / fx * cam_xyzs[:, :, 2] + half_grid_size * 2
                                mask_y = torch.abs(cam_xyzs[:, :, 1]) < cy / fy * cam_xyzs[:, :, 2] + half_grid_size * 2
                                mask = (mask_z & mask_x & mask_y).sum(0).reshape(-1) # [N]

                                # update count 
                                count[cas, indices] += mask
                                head += S
    
        # mark untrained grid as -1
        self.density_grid[count == 0] = -1

        #print(f'[mark untrained grid] {(count == 0).sum()} from {resolution ** 3 * self.cascade}')

    @torch.no_grad()
    def update_extra_state(self, decay=0.95, S=128):
        # call before each epoch to update extra states.

        if not self.cuda_ray:
            return 
        
        ### update density grid

        tmp_grid = - torch.ones_like(self.density_grid)
        
        # full update.
        if self.iter_density < 16:
        #if True:
            X = torch.arange(self.grid_size, dtype=torch.int32, device=self.density_bitfield.device).split(S)
            Y = torch.arange(self.grid_size, dtype=torch.int32, device=self.density_bitfield.device).split(S)
            Z = torch.arange(self.grid_size, dtype=torch.int32, device=self.density_bitfield.device).split(S)

            for xs in X:
                for ys in Y:
                    for zs in Z:
                        
                        # construct points
                        xx, yy, zz = custom_meshgrid(xs, ys, zs)
                        coords = torch.cat([xx.reshape(-1, 1), yy.reshape(-1, 1), zz.reshape(-1, 1)], dim=-1) # [N, 3], in [0, 128)
                        indices = raymarching.morton3D(coords).long() # [N]
                        xyzs = 2 * coords.float() / (self.grid_size - 1) - 1 # [N, 3] in [-1, 1]

                        # cascading
                        for cas in range(self.cascade):
                            bound = min(2 ** cas, self.bound)
                            half_grid_size = bound / self.grid_size
                            # scale to current cascade's resolution
                            cas_xyzs = xyzs * (bound - half_grid_size)
                            # add noise in [-hgs, hgs]
                            cas_xyzs += (torch.rand_like(cas_xyzs) * 2 - 1) * half_grid_size
                            # query density
                            sigmas = self.density(cas_xyzs)['sigma'].reshape(-1).detach()
                            sigmas *= self.density_scale
                            # assign 
                            tmp_grid[cas, indices] = sigmas

        # partial update (half the computation)
        # TODO: why no need of maxpool ?
        else:
            N = self.grid_size ** 3 // 4 # H * H * H / 4
            for cas in range(self.cascade):
                # random sample some positions
                coords = torch.randint(0, self.grid_size, (N, 3), device=self.density_bitfield.device) # [N, 3], in [0, 128)
                indices = raymarching.morton3D(coords).long() # [N]
                # random sample occupied positions
                occ_indices = torch.nonzero(self.density_grid[cas] > 0).squeeze(-1) # [Nz]
                rand_mask = torch.randint(0, occ_indices.shape[0], [N], dtype=torch.long, device=self.density_bitfield.device)
                occ_indices = occ_indices[rand_mask] # [Nz] --> [N], allow for duplication
                occ_coords = raymarching.morton3D_invert(occ_indices) # [N, 3]
                # concat
                indices = torch.cat([indices, occ_indices], dim=0)
                coords = torch.cat([coords, occ_coords], dim=0)
                # same below
                xyzs = 2 * coords.float() / (self.grid_size - 1) - 1 # [N, 3] in [-1, 1]
                bound = min(2 ** cas, self.bound)
                half_grid_size = bound / self.grid_size
                # scale to current cascade's resolution
                cas_xyzs = xyzs * (bound - half_grid_size)
                # add noise in [-hgs, hgs]
                cas_xyzs += (torch.rand_like(cas_xyzs) * 2 - 1) * half_grid_size
                # query density
                sigmas = self.density(cas_xyzs)['sigma'].reshape(-1).detach()
                sigmas *= self.density_scale
                # assign 
                tmp_grid[cas, indices] = sigmas

        ## max-pool on tmp_grid for less aggressive culling [No significant improvement...]
        # invalid_mask = tmp_grid < 0
        # tmp_grid = F.max_pool3d(tmp_grid.view(self.cascade, 1, self.grid_size, self.grid_size, self.grid_size), kernel_size=3, stride=1, padding=1).view(self.cascade, -1)
        # tmp_grid[invalid_mask] = -1

        # ema update
        valid_mask = (self.density_grid >= 0) & (tmp_grid >= 0)
        self.density_grid[valid_mask] = torch.maximum(self.density_grid[valid_mask] * decay, tmp_grid[valid_mask])
        self.mean_density = torch.mean(self.density_grid.clamp(min=0)).item() # -1 non-training regions are viewed as 0 density.
        self.iter_density += 1

        # convert to bitfield
        density_thresh = min(self.mean_density, self.density_thresh)
        self.density_bitfield = raymarching.packbits(self.density_grid, density_thresh, self.density_bitfield)

        ### update step counter
        total_step = min(16, self.local_step)
        if total_step > 0:
            self.mean_count = int(self.step_counter[:total_step, 0].sum().item() / total_step)
        self.local_step = 0

        #print(f'[density grid] min={self.density_grid.min().item():.4f}, max={self.density_grid.max().item():.4f}, mean={self.mean_density:.4f}, occ_rate={(self.density_grid > 0.01).sum() / (128**3 * self.cascade):.3f} | [step counter] mean={self.mean_count}')


    def render(self, rays_o, rays_d, staged=False, max_ray_batch=4096, 
               datatype='rgb', max_far=5, min_near=.2, **kwargs):
        # rays_o, rays_d: [B, N, 3], assumes B == 1
        # return: pred_rgb: [B, N, 3]

        # if datatype != "viewer":
        #     print("ray origins")
        #     print(rays_o)

        if self.cuda_ray:
            _run = self.run_cuda
        else:
            _run = self.run

        #print("run funct")
        #print(_run)

        B, N = rays_o.shape[:2]
        device = rays_o.device

        #print("CUDA RAY DEC")
        #print(self.cuda_ray)

        # never stage when cuda_ray
        if staged and not self.cuda_ray:
            depth = torch.empty((B, N), device=device)
            image = torch.empty((B, N, 3), device=device)

            for b in range(B):
                head = 0
                while head < N:
                    tail = min(head + max_ray_batch, N)
                    results_ = _run(rays_o[b:b+1, head:tail], rays_d[b:b+1, head:tail],
                                    datatype=datatype, max_far=max_far, min_near=min_near, **kwargs)
                    depth[b:b+1, head:tail] = results_['depth']
                    image[b:b+1, head:tail] = results_['image']
                    head += max_ray_batch
            
            results = {}
            results['depth'] = depth
            results['image'] = image

        else:
            results = _run(rays_o, rays_d, datatype=datatype,
                           max_far=max_far, min_near=min_near, **kwargs)

        return results
