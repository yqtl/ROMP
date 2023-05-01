import os,sys
import torch
import numpy as np

#import config
#import constants
from smplx import SMPL
# Part of the codes are brought from https://github.com/mkocabas/VIBE/blob/master/lib/utils/eval_utils.py

def _calc_matched_PCKh_(real, pred, kp2d_mask, error_thresh=0.143):
    # error_thresh is set as the ratio between the head and the body.
    # he head / body for normal people are between 6~8, therefore, we set it to 1/7=0.143
    PCKs = torch.ones(len(kp2d_mask)).float().cuda()*-1.
    if kp2d_mask.sum()>0:
        vis = (real>-1.).sum(-1)==real.shape[-1]
        error = torch.norm(real-pred, p=2, dim=-1)
        
        for ind, (e, v) in enumerate(zip(error, vis)):
            if v.sum() < 2:
                continue
            real_valid = real[ind,v]
            person_scales = torch.sqrt((real_valid[:,0].max(-1).values - real_valid[:,0].min(-1).values)**2 + \
                            (real_valid[:,1].max(-1).values - real_valid[:,1].min(-1).values)**2)
            error_valid = e[v]
            correct_kp_mask = ((error_valid / person_scales) < error_thresh).float()
            PCKs[ind] = correct_kp_mask.sum()/len(correct_kp_mask)
    return PCKs

def _calc_relative_age_error_weak_(age_preds, age_gts, matched_mask=None):
    valid_mask = age_gts != -1
    if matched_mask is not None:
        valid_mask *= matched_mask
    error_dict = {age_name:[] for age_name in constants.relative_age_types}
    if valid_mask.sum()>0:
        for age_id, age_name in enumerate(constants.relative_age_types):
            age_gt = age_gts[valid_mask].long() == age_id
            age_pred = age_preds[valid_mask][age_gt].long() # == age_id  
            error_dict.update({age_name: [age_pred]})
    return error_dict

def _calc_absolute_depth_error(trans_preds, trans_gt):
    trans_error = np.sqrt(((trans_gt - trans_preds)**2).sum(-1))
    return trans_error

def _calc_relative_depth_error_weak_(pred_depths, depth_ids, reorganize_idx, age_gts=None, thresh=0.2, matched_mask=None):
    depth_ids = depth_ids.to(pred_depths.device)
    depth_ids_vmask = depth_ids != -1
    pred_depths_valid = pred_depths[depth_ids_vmask]
    valid_inds = reorganize_idx[depth_ids_vmask]
    depth_ids = depth_ids[depth_ids_vmask]
    age_gts = age_gts[depth_ids_vmask]
    error_dict = {'eq': [], 'cd': [], 'fd':[], 'eq_age': [], 'cd_age': [], 'fd_age':[]}
    error_each_age = {age_type:[] for age_type in constants.relative_age_types}
    for b_ind in torch.unique(valid_inds):
        sample_inds = valid_inds == b_ind
        if matched_mask is not None:
            sample_inds *= matched_mask[depth_ids_vmask]
        did_num = sample_inds.sum()
        if did_num > 1:
            pred_depths_sample = pred_depths_valid[sample_inds]
            triu_mask = torch.triu(torch.ones(did_num, did_num), diagonal=1).bool()
            dist_mat = (pred_depths_sample.unsqueeze(0).repeat(did_num, 1) - pred_depths_sample.unsqueeze(1).repeat(1,did_num))[triu_mask]
            did_mat = (depth_ids[sample_inds].unsqueeze(0).repeat(did_num, 1) - depth_ids[sample_inds].unsqueeze(1).repeat(1,did_num))[triu_mask]
            
            error_dict['eq'].append(dist_mat[did_mat==0])
            error_dict['cd'].append(dist_mat[did_mat<0])
            error_dict['fd'].append(dist_mat[did_mat>0])
            if age_gts is not None:
                age_sample = age_gts[sample_inds]
                age_mat = torch.cat([age_sample.unsqueeze(0).repeat(did_num, 1).unsqueeze(-1), age_sample.unsqueeze(1).repeat(1, did_num).unsqueeze(-1)], -1)[triu_mask]
                error_dict['eq_age'].append(age_mat[did_mat==0])
                error_dict['cd_age'].append(age_mat[did_mat<0])
                error_dict['fd_age'].append(age_mat[did_mat>0])

    return error_dict


def _calc_relative_depth_error_withgts_(pred_depths, depth_gts, reorganize_idx, age_gts=None, thresh=0.3, matched_mask=None):
    depth_gts = depth_gts.to(pred_depths.device)
    error_dict = {'eq': [], 'cd': [], 'fd':[], 'eq_age': [], 'cd_age': [], 'fd_age':[]}
    for b_ind in torch.unique(reorganize_idx):
        sample_inds = reorganize_idx == b_ind
        if matched_mask is not None:
            sample_inds *= matched_mask
        did_num = sample_inds.sum()
        if did_num > 1:
            pred_depths_sample = pred_depths[sample_inds]
            triu_mask = torch.triu(torch.ones(did_num, did_num), diagonal=1).bool()
            dist_mat = (pred_depths_sample.unsqueeze(0).repeat(did_num, 1) - pred_depths_sample.unsqueeze(1).repeat(1,did_num))[triu_mask]
            dist_mat_gt = (depth_gts[sample_inds].unsqueeze(0).repeat(did_num, 1) - depth_gts[sample_inds].unsqueeze(1).repeat(1,did_num))[triu_mask]
            
            error_dict['eq'].append(dist_mat[torch.abs(dist_mat_gt)<thresh])
            error_dict['cd'].append(dist_mat[dist_mat_gt<-thresh])
            error_dict['fd'].append(dist_mat[dist_mat_gt>thresh])
            if age_gts is not None:
                age_sample = age_gts[sample_inds]
                age_mat = torch.cat([age_sample.unsqueeze(0).repeat(did_num, 1).unsqueeze(-1), age_sample.unsqueeze(1).repeat(1, did_num).unsqueeze(-1)], -1)[triu_mask]
                error_dict['eq_age'].append(age_mat[torch.abs(dist_mat_gt)<thresh])
                error_dict['cd_age'].append(age_mat[dist_mat_gt<-thresh])
                error_dict['fd_age'].append(age_mat[dist_mat_gt>thresh])

    return error_dict


def compute_error_verts(pred_theta=None, target_theta=None, target_verts=None, pred_verts=None, smpl_path=None):
    """
    brought from https://github.com/mkocabas/VIBE/blob/master/lib/utils/eval_utils.py
    Computes MPJPE over 6890 surface vertices.
    Args:
        verts_gt (Nx6890x3).
        verts_pred (Nx6890x3).
    Returns:
        error_verts (N).
    """
    if target_verts is None:
        target_verts = get_verts(target_theta, smpl_path)    #os.path.join(config.model_dir,'smpl_models','smpl')
    if pred_verts is None:
        pred_verts = get_verts(pred_theta, smpl_path)        

    assert len(pred_verts) == len(target_verts)
    error_per_vert = np.sqrt(np.sum((target_verts - pred_verts) ** 2, axis=2))
    return np.mean(error_per_vert, axis=1)

def get_verts(theta, smpl_path):
    device = 'cpu'
    smpl = SMPL(smpl_path,batch_size=1).to(device)

    pose, betas = theta[:,:72], theta[:,72:]

    verts = []
    b_ = torch.split(betas, 5000)
    p_ = torch.split(pose, 5000)

    for b,p in zip(b_,p_):
        output = smpl(betas=b, body_pose=p[:, 3:], global_orient=p[:, :3], pose2rot=True)
        verts.append(output.vertices.detach().cpu().numpy())

    verts = np.concatenate(verts, axis=0)
    del smpl
    return verts

def compute_similarity_transform(S1, S2):
    '''
    Computes a similarity transform (sR, t) that takes
    a set of 3D points S1 (3 x N) closest to a set of 3D points S2,
    where R is an 3x3 rotation matrix, t 3x1 translation, s scale.
    i.e. solves the orthogonal Procrutes problem.
    '''
    transposed = False
    if S1.shape[0] != 3 and S1.shape[0] != 2:
        S1 = S1.T
        S2 = S2.T
        transposed = True
    assert(S2.shape[1] == S1.shape[1])

    # 1. Remove mean.
    mu1 = S1.mean(axis=1, keepdims=True)
    mu2 = S2.mean(axis=1, keepdims=True)
    X1 = S1 - mu1
    X2 = S2 - mu2

    # 2. Compute variance of X1 used for scale.
    var1 = np.sum(X1**2)

    # 3. The outer product of X1 and X2.
    K = X1.dot(X2.T)

    # 4. Solution that Maximizes trace(R'K) is R=U*V', where U, V are
    # singular vectors of K.
    U, s, Vh = np.linalg.svd(K)
    V = Vh.T
    # Construct Z that fixes the orientation of R to get det(R)=1.
    Z = np.eye(U.shape[0])
    Z[-1, -1] *= np.sign(np.linalg.det(U.dot(V.T)))
    # Construct R.
    R = V.dot(Z.dot(U.T))

    # 5. Recover scale.
    scale = np.trace(R.dot(K)) / var1

    # 6. Recover translation.
    t = mu2 - scale*(R.dot(mu1))

    # 7. Error:
    S1_hat = scale*R.dot(S1) + t

    if transposed:
        S1_hat = S1_hat.T

    return S1_hat


def compute_similarity_transform_torch(S1, S2):
    '''
    Computes a similarity transform (sR, t) that takes
    a set of 3D points S1 (3 x N) closest to a set of 3D points S2,
    where R is an 3x3 rotation matrix, t 3x1 translation, s scale.
    i.e. solves the orthogonal Procrutes problem.
    '''
    S1, S2 = S1.float(), S2.float()
    transposed = False
    if S1.shape[0] != 3 and S1.shape[0] != 2:
        S1 = S1.T
        S2 = S2.T
        transposed = True
    assert (S2.shape[1] == S1.shape[1])

    # 1. Remove mean.
    mu1 = S1.mean(axis=1, keepdims=True)
    mu2 = S2.mean(axis=1, keepdims=True)
    X1 = S1 - mu1
    X2 = S2 - mu2

    # print('X1', X1.shape)

    # 2. Compute variance of X1 used for scale.
    var1 = torch.sum(X1 ** 2)

    # print('var', var1.shape)

    # 3. The outer product of X1 and X2.
    K = X1.mm(X2.T)

    # 4. Solution that Maximizes trace(R'K) is R=U*V', where U, V are
    # singular vectors of K.
    U, s, V = torch.svd(K)
    # V = Vh.T
    # Construct Z that fixes the orientation of R to get det(R)=1.
    Z = torch.eye(U.shape[0], device=S1.device)
    Z[-1, -1] *= torch.sign(torch.det(U @ V.T))
    # Construct R.
    R = V.mm(Z.mm(U.T))

    # print('R', X1.shape)

    # 5. Recover scale.
    scale = torch.trace(R.mm(K)) / var1
    # print(R.shape, mu1.shape)
    # 6. Recover translation.
    t = mu2 - scale * (R.mm(mu1))
    # print(t.shape)

    # 7. Error:
    S1_hat = scale * R.mm(S1) + t

    if transposed:
        S1_hat = S1_hat.T

    return S1_hat


def batch_compute_similarity_transform_torch(S1, S2):
    '''
    Computes a similarity transform (sR, t) that takes
    a set of 3D points S1 (3 x N) closest to a set of 3D points S2,
    where R is an 3x3 rotation matrix, t 3x1 translation, s scale.
    i.e. solves the orthogonal Procrutes problem.
    '''
    transposed = False
    if S1.shape[0] != 3 and S1.shape[0] != 2:
        S1 = S1.permute(0,2,1)
        S2 = S2.permute(0,2,1)
        transposed = True
    assert(S2.shape[1] == S1.shape[1])

    # 1. Remove mean.
    mu1 = S1.mean(axis=-1, keepdims=True)
    mu2 = S2.mean(axis=-1, keepdims=True)

    X1 = S1 - mu1
    X2 = S2 - mu2

    # 2. Compute variance of X1 used for scale.
    var1 = torch.sum(X1**2, dim=1).sum(dim=1)

    # 3. The outer product of X1 and X2.
    K = X1.bmm(X2.permute(0,2,1))

    # 4. Solution that Maximizes trace(R'K) is R=U*V', where U, V are
    # singular vectors of K.
    U, s, V = torch.svd(K)

    # Construct Z that fixes the orientation of R to get det(R)=1.
    Z = torch.eye(U.shape[1], device=S1.device).unsqueeze(0)
    Z = Z.repeat(U.shape[0],1,1)
    Z[:,-1, -1] *= torch.sign(torch.det(U.bmm(V.permute(0,2,1))))

    # Construct R.
    R = V.bmm(Z.bmm(U.permute(0,2,1)))

    # 5. Recover scale.
    scale = torch.cat([torch.trace(x).unsqueeze(0) for x in R.bmm(K)]) / var1

    # 6. Recover translation.
    t = mu2 - (scale.unsqueeze(-1).unsqueeze(-1) * (R.bmm(mu1)))

    # 7. Error:
    S1_hat = scale.unsqueeze(-1).unsqueeze(-1) * R.bmm(S1) + t

    if transposed:
        S1_hat = S1_hat.permute(0,2,1)

    return S1_hat, (scale, R, t)


def compute_mpjpe(predicted, target, valid_mask=None, pck_joints=None, sample_wise=True):
    """
    Mean per-joint position error (i.e. mean Euclidean distance),
    often referred to as "Protocol #1" in many papers.
    """
    assert predicted.shape == target.shape, print(predicted.shape, target.shape)
    mpjpe = torch.norm(predicted - target, p=2, dim=-1)
    
    if pck_joints is None:
        if sample_wise:
            mpjpe_batch = (mpjpe*valid_mask.float()).sum(-1)/valid_mask.float().sum(-1) if valid_mask is not None else mpjpe.mean(-1)
        else:
            mpjpe_batch = mpjpe[valid_mask] if valid_mask is not None else mpjpe
        return mpjpe_batch
    else:
        mpjpe_pck_batch = mpjpe[:,pck_joints]
        return mpjpe_pck_batch


#### old code 

def p_mpjpe(predicted, target, with_sRt=False,full_torch=False,with_aligned=False,each_separate=False):
    """
    Pose error: MPJPE after rigid alignment (scale, rotation, and translation),
    often referred to as "Protocol #2" in many papers.
    """
    assert predicted.shape == target.shape

    muX = np.mean(target, axis=1, keepdims=True)
    muY = np.mean(predicted, axis=1, keepdims=True)

    X0 = target - muX
    Y0 = predicted - muY

    normX = np.sqrt(np.sum(X0**2, axis=(1, 2), keepdims=True))
    normY = np.sqrt(np.sum(Y0**2, axis=(1, 2), keepdims=True))

    X0 /= (normX+1e-6)
    Y0 /= (normY+1e-6)


    H = np.matmul(X0.transpose(0, 2, 1), Y0).astype(np.float16).astype(np.float64)
    U, s, Vt = np.linalg.svd(H)
    V = Vt.transpose(0, 2, 1)
    R = np.matmul(V, U.transpose(0, 2, 1))

    # Avoid improper rotations (reflections), i.e. rotations with det(R) = -1
    sign_detR = np.sign(np.expand_dims(np.linalg.det(R), axis=1))
    V[:, :, -1] *= sign_detR
    s[:, -1] *= sign_detR.flatten()
    R = np.matmul(V, U.transpose(0, 2, 1)) # Rotation

    tr = np.expand_dims(np.sum(s, axis=1, keepdims=True), axis=2)

    a = tr * normX / normY # Scale
    t = muX - a*np.matmul(muY, R) # Translation

    # Perform rigid transformation on the input
    predicted_aligned = a*np.matmul(predicted, R) + t
    if each_separate:
        return np.linalg.norm(predicted_aligned - target, axis=len(target.shape)-1)

    error = np.mean(np.linalg.norm(predicted_aligned - target, axis=len(target.shape)-1))
    if with_sRt and not with_aligned:
        return error, (a,R,t)
    if with_aligned:
        return error,(a,R,t),predicted_aligned
    # Return MPJPE
    return error

def compute_errors(gt3ds, preds):
    """
    Gets MPJPE after pelvis alignment + MPJPE after Procrustes.
    Evaluates on the 14 common joints.
    Inputs:
      - gt3ds: N x 14 x 3
      - preds: N x 14 x 3
    """
    errors, errors_pa = [], []
    for i, (gt3d, pred) in enumerate(zip(gt3ds, preds)):
        gt3d = gt3d.reshape(-1, 3)
        # Root align.
        gt3d = align_by_pelvis(gt3d)
        pred3d = align_by_pelvis(pred)

        joint_error = np.sqrt(np.sum((gt3d - pred3d)**2, axis=1))
        errors.append(np.mean(joint_error))

        # Get PA error.
        pred3d_sym = compute_similarity_transform(pred3d, gt3d)
        pa_error = np.sqrt(np.sum((gt3d - pred3d_sym)**2, axis=1))
        errors_pa.append(np.mean(pa_error))

    return errors, errors_pa

def n_mpjpe(predicted, target):
    """
    Normalized MPJPE (scale only), adapted from:
    https://github.com/hrhodin/UnsupervisedGeometryAwareRepresentationLearning/blob/master/losses/poses.py
    """
    assert predicted.shape == target.shape

    norm_predicted = torch.mean(torch.sum(predicted**2, dim=3, keepdim=True), dim=2, keepdim=True)
    norm_target = torch.mean(torch.sum(target*predicted, dim=3, keepdim=True), dim=2, keepdim=True)
    scale = norm_target / norm_predicted
    return mpjpe(scale * predicted, target)

def mean_velocity_error(predicted, target):
    """
    Mean per-joint velocity error (i.e. mean Euclidean distance of the 1st derivative)
    """
    assert predicted.shape == target.shape

    velocity_predicted = np.diff(predicted, axis=0)
    velocity_target = np.diff(target, axis=0)

    return np.mean(np.linalg.norm(velocity_predicted - velocity_target, axis=len(target.shape)-1))

def compute_accel(joints):
    """
    Computes acceleration of 3D joints.
    Args:
        joints (Nx25x3).
    Returns:
        Accelerations (N-2).
    """
    velocities = joints[1:] - joints[:-1]
    acceleration = velocities[1:] - velocities[:-1]
    acceleration_normed = np.linalg.norm(acceleration, axis=2)
    return np.mean(acceleration_normed, axis=1)

def compute_error_accel(joints_gt, joints_pred, vis=None):
    """
    Computes acceleration error:
        1/(n-2) \sum_{i=1}^{n-1} X_{i-1} - 2X_i + X_{i+1}
    Note that for each frame that is not visible, three entries in the
    acceleration error should be zero'd out.
    Args:
        joints_gt (Nx14x3).
        joints_pred (Nx14x3).
        vis (N).
    Returns:
        error_accel (N-2).
    """
    # (N-2)x14x3
    accel_gt = joints_gt[:-2] - 2 * joints_gt[1:-1] + joints_gt[2:]
    accel_pred = joints_pred[:-2] - 2 * joints_pred[1:-1] + joints_pred[2:]

    normed = np.linalg.norm(accel_pred - accel_gt, axis=2)

    if vis is None:
        new_vis = np.ones(len(normed), dtype=bool)
    else:
        invis = np.logical_not(vis)
        invis1 = np.roll(invis, -1)
        invis2 = np.roll(invis, -2)
        new_invis = np.logical_or(invis, np.logical_or(invis1, invis2))[:-2]
        new_vis = np.logical_not(new_invis)

    return np.mean(normed[new_vis], axis=1)



def test():
    for i in range(100):
        r1 = np.random.rand(3,14,3)
        r2 = np.random.rand(3,14,3)
        predicted = np.random.rand(3,14,3)
        target = np.random.rand(3,14,3)
        #add mask to the end dim of target, so that the last dim is 4, set last dim to 1
        target = np.concatenate((target,np.ones((3,14,1))),axis=2)
        pmpjpe = p_mpjpe(r1, r2,with_sRt=False)
        #pmpjpe_torch = p_mpjpe_torch(torch.from_numpy(r1), torch.from_numpy(r2),with_sRt=False,full_torch=True)
        #print('pmpjpe: {}; {:.6f}; {:.6f}; {:.6f}'.format(pmpjpe==pmpjpe_torch.numpy(),pmpjpe,pmpjpe_torch.numpy(), pmpjpe-pmpjpe_torch.numpy()))
        print('pmpjpe: {:.6f}'.format(pmpjpe))
        #ordinary_mpjpe = compute_mpjpe(r1, r2)
        #print('ordinary_mpjpe: {:.6f}'.format(ordinary_mpjpe))
        normalized_mpjpe = n_mpjpe(predicted, target)
        '''
        pmpjpe,(s,R,t),(H,U, s, Vt) = p_mpjpe(r1, r2,with_sRt=True)
        pmpjpe_torch,(s_torch,R_torch,t_torch),(H_torch,U_torch, s_torch, Vt_torch) = p_mpjpe_torch(torch.from_numpy(r1), torch.from_numpy(r2),with_sRt=True,full_torch=True)
        print('s:',s==s_torch.numpy(),s,s_torch.numpy())
        print('R:',R==R_torch.numpy(),R,R_torch.numpy())
        print('t:',t==t_torch.numpy(),t,t_torch.numpy())
        print(H)
        print(H_torch)
        print(U)
        print(U_torch)
        print(Vt)
        print(Vt_torch)
        print(s)
        print(s_torch)
        '''

if __name__ == '__main__':
    test()
