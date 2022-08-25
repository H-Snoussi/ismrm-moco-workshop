import numpy as np
#from gpuNUFFT import NUFFTOp
from merlintf.complex import *
from merlintf.keras.layers.data_consistency import itSENSE, DCPM
from merlintf.keras.layers.mri import MulticoilForwardOp, MulticoilAdjointOp
from mri.operators import NonCartesianFFT
from utils.motioncomp import *
import tensorflow as tf


def rss(coil_img):
    # root sum-of-squares coil combination
    return np.sqrt(np.sum(np.abs(coil_img)**2, -1))


def minmaxscale(x, scale):
    # x     to be scaled image
    # scale [min, max]
    return ((x-np.amin(x)) * (scale[1]-scale[0]))/(np.amax(x) - np.amin(x))


def maxscale(img):
    return img/np.amax(np.abs(img))


# Cartesian 2D operators
def mriAdjointOp(kspace, smaps, mask):
    return np.sum(ifft2c(kspace * mask)*np.conj(smaps), axis=-1)

def mriForwardOp(image, smaps, mask):
    return fft2c(smaps * image[:,:,np.newaxis]) * mask

def fft2c(image, axes=(0,1)):
    return np.fft.fftshift(np.fft.fft2(np.fft.ifftshift(image, axes=axes), norm='ortho', axes=axes), axes=axes)

def ifft2c(kspace, axes=(0,1)):
    return np.fft.fftshift(np.fft.ifft2(np.fft.ifftshift(kspace, axes=axes), norm='ortho', axes=axes), axes=axes)

# Define Batchelor's motion operator
def BatchForwardOp(image, smaps, masks, motions):
    Nx = np.shape(image)[0]
    Ny = np.shape(image)[1]
    Nc = np.shape(smaps)[2]
    Nt = np.shape(masks)[2]

    kspace_out = np.zeros((Nx,Ny,Nc,Nt)) + 1j * np.zeros((Nx,Ny,Nc,Nt))
    for t in range(Nt):
        im_aux = apply_sparse_motion(image,get_sparse_motion_matrix(motions[:,:,:,t]),0)
        kspace_out[:,:,:,t] = mriForwardOp(im_aux, smaps, masks[:,:,t][:,:,np.newaxis])
    return np.sum(kspace_out,3)

def BatchAdjointOp(kspace, smaps, masks, motions):
    Nx = np.shape(kspace)[0]
    Ny = np.shape(kspace)[1]
    Nc = np.shape(smaps)[2]
    Nt = np.shape(masks)[2]

    image_out = np.zeros((Nx,Ny,Nt)) + 1j * np.zeros((Nx,Ny,Nt))
    #im_aux = np.zeros((Nx,Ny,Nc))
    for t in range(Nt):
        im_aux = mriAdjointOp(kspace, smaps, masks[:,:,t][:,:,np.newaxis])
        image_out[:,:,t] = apply_sparse_motion(im_aux,get_sparse_motion_matrix(motions[:,:,:,t]),1)
    return np.sum(image_out,2)

class BatchelorFwd(tf.keras.layers.Layer):
    def __init__(self):
        super().__init__()
        self.op = BatchForwardOp

    def call(self, image, smaps, mask, flow):
        return numpy2tensor(self.op(np.squeeze(image.numpy()), np.squeeze(smaps.numpy()), np.squeeze(mask.numpy()),
                                    np.squeeze(flow.numpy())))

class BatchelorAdj(tf.keras.layers.Layer):
    def __init__(self):
        super().__init__()
        self.op = BatchAdjointOp

    def call(self, kspace, smaps, mask, flow):
        return numpy2tensor(self.op(np.squeeze(kspace.numpy()), np.squeeze(smaps.numpy()), np.squeeze(mask.numpy()),
                                    np.squeeze(flow.numpy())))

# Non-Cartesian 2D operators
class GPUNUFFTFwd(tf.keras.layers.Layer):
    def __init__(self, nRead, csm, traj, dcf):
        super().__init__()
        self.op = NonCartesianFFT(samples=traj, shape=[nRead, nRead], n_coils=np.shape(csm)[0], density_comp=dcf,
                                smaps=csm, implementation='gpuNUFFT')

    def call(self, x, csm, traj, dcf):
        return numpy2tensor(self.op.op(np.squeeze(x.numpy())), add_batch_dim=True, add_channel_dim=False)


class GPUNUFFTAdj(tf.keras.layers.Layer):
    def __init__(self, nRead, csm, traj, dcf):
        super().__init__()
        self.op = NonCartesianFFT(samples=traj, shape=[nRead, nRead], n_coils=np.shape(csm)[0], density_comp=dcf, smaps=csm,
                              implementation='gpuNUFFT')

    def call(self, x, csm, traj, dcf):
        return numpy2tensor(self.op.adj_op(np.squeeze(x.numpy())), add_batch_dim=True, add_channel_dim=False)


class BatchelorGPUNUFFTFwd(tf.keras.layers.Layer):
    def __init__(self, nRead, csm, traj, dcf):
        super().__init__()
        self.Nx = nRead
        self.Ny = nRead
        self.Nc = np.shape(csm)[0]
        self.Nt = np.shape(traj)[2]
        self.NSpokes = np.shape(traj)[0]
        self.op = NonCartesianFFT(samples=traj, shape=[nRead, nRead], n_coils=np.shape(csm)[0], density_comp=dcf,
                                smaps=csm, implementation='gpuNUFFT')

    def call(self, x, csm, traj, dcf, flow):
        image = np.squeeze(x.numpy())
        motions = np.squeeze(flow.numpy())

        kspace_out = np.zeros((self.Nx, self.NSpokes, self.Nc, self.Nt)) + 1j * np.zeros((self.Nx, self.NSpokes, self.Nc, self.Nt))
        for t in range(self.Nt):
            im_aux = apply_sparse_motion(image, get_sparse_motion_matrix(motions[:, :, :, t]), 0)
            kspace_out[:, :, :, t] = self.op.op(im_aux)
        return numpy2tensor(np.sum(kspace_out, 3), add_batch_dim=True, add_channel_dim=False)


class BatchelorGPUNUFFTAdj(tf.keras.layers.Layer):
    def __init__(self, nRead, csm, traj, dcf):
        super().__init__()
        self.Nx = nRead
        self.Ny = nRead
        self.Nc = np.shape(csm)[0]
        self.Nt = np.shape(traj)[2]
        self.NSpokes = np.shape(traj)[0]
        self.op = NonCartesianFFT(samples=traj, shape=[nRead, nRead], n_coils=np.shape(csm)[0], density_comp=dcf,
                                  smaps=csm, implementation='gpuNUFFT')

    def call(self, x, csm, traj, dcf, flow):
        kspace = np.squeeze(x.numpy())
        motions = np.squeeze(flow.numpy())

        image_out = np.zeros((self.Nx, self.Ny, self.Nt)) + 1j * np.zeros((self.Nx, self.Ny, self.Nt))
        for t in range(self.Nt):
            im_aux = self.op.adj_op(kspace)
            image_out[:, :, t] = apply_sparse_motion(im_aux, get_sparse_motion_matrix(motions[:, :, :, t]), 1)
        return numpy2tensor(np.sum(image_out, 2), add_batch_dim=True, add_channel_dim=False)


def iterativeSENSE(kspace, smap=None, mask=None, noisy=None, dcf=None, flow=None,
                     fwdop=MulticoilForwardOp, adjop=MulticoilAdjointOp,
                     add_batch_dim=True, max_iter=10, tol=1e-12, weight_init=1.0, weight_scale=1.0):
    # kspace        raw k-space data as [batch, coils, X, Y] or [batch, coils, X, Y, Z] or [batch, coils, time, X, Y] or [batch, coils, time, X, Y, Z] (numpy array)
    # smap          coil sensitivity maps with same shape as kspace (or singleton dimension for time) (numpy array)
    # mask          subsampling including/excluding soft-weights with same shape as kspace (numpy array)
    # noisy         initialiaztion for reconstructed image, if None it is created from A^H(kspace) (numpy array)
    # dcf           density compensation function (only non-Cartesian) (numpy array)
    # flow          flow field (numpy array)
    # fwdop         forward operator A
    # adjop         adjoint operator A^H
    # add_batch_dim automatically append batch dimension for GPU execution
    # max_iter      maximum number of iterations for CG/iterative SENSE
    # tol           tolerance for stopping condition for CG/iterative SENSE
    # weight_init   initial weighting for lambda regularization parameter
    # weight_scale  scaling factor for lambda regularization parameter
    # return:       reconstructed image (numpy array)

    if dcf is not None:
        bradial = True
    else:
        bradial = False

    if flow is not None:
        motioncomp = True
    else
        motioncomp = False

    # Forward and Adjoint operators
    A = fwdop
    AH = adjop

    if type(kspace).__module__ == np.__name__:
        kspace = numpy2tensor(kspace, add_batch_dim=add_batch_dim, add_channel_dim=False)
    if smap is not None and type(smap).__module__ == np.__name__:
        smap = numpy2tensor(smap, add_batch_dim=add_batch_dim, add_channel_dim=False)
    if mask is not None and type(mask).__module__ == np.__name__:
        mask = numpy2tensor(mask, add_batch_dim=add_batch_dim, add_channel_dim=False)
    if dcf is not None and type(dcf).__module__ == np.__name__:
        dcf = numpy2tensor(dcf, add_batch_dim=add_batch_dim, add_channel_dim=False)
    if flow is not None and type(flow).__module__ == np.__name__:
        flow = numpy2tensor(flow, add_batch_dim=add_batch_dim, add_channel_dim=False)

    if noisy is None:
        if bradial:
            if motioncomp:
                noisy = AH(kspace, smap, mask, dcf, flow)
            else:
                noisy = AH(kspace, smap, mask, dcf)
        else:
            if motioncomp:
                noisy = AH(kspace, mask, smap, flow)
            else:
                noisy = AH(kspace, mask, smap)
    elif noisy is not None and type(noisy).__module__ == np.__name__:
        noisy = numpy2tensor(noisy, add_batch_dim=add_batch_dim, add_channel_dim=False)

    model = DCPM(A, AH, weight_init=weight_init, weight_scale=weight_scale, max_iter=max_iter, tol=tol)
    if bradial:  # non-Cartesian
        if motioncomp:
            return np.squeeze(model([noisy, kspace, smap, mask, dcf, flow]).numpy())
        else:
            return np.squeeze(model([noisy, kspace, smap, mask, dcf]).numpy())
    else:  # Cartesian
        if motioncomp:
            return np.squeeze(model([noisy, kspace, mask, smap, flow]).numpy())
        else:
            return np.squeeze(model([noisy, kspace, mask, smap]).numpy())