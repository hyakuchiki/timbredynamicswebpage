import torch
import torch.nn as nn
import numpy as np
import torch.nn.functional as F
import librosa
from torchaudio.transforms import MelScale
from torchaudio.functional import create_dct
from ddspsynth.util import log_eps, pad_or_trim_to_expected_length
import crepe

amp = lambda x: x[...,0]**2 + x[...,1]**2

class MelSpec(nn.Module):
    def __init__(self, n_fft=2048, hop_length=1024, n_mels=128, sample_rate=16000, power=1, f_min=40, f_max=7600, pad_end=True, center=False):
        """
        
        """
        super().__init__()
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.power = power
        self.f_min = f_min
        self.f_max = f_max
        self.sample_rate = sample_rate
        self.n_mels = n_mels
        self.pad_end = pad_end
        self.center = center
        self.mel_scale = MelScale(self.n_mels, self.sample_rate, self.f_min, self.f_max, self.n_fft // 2 + 1)
    
    def forward(self, audio):
        if self.pad_end:
            _batch_dim, l_x = audio.shape
            remainder = (l_x - self.n_fft) % self.hop_length
            pad = 0 if (remainder == 0) else self.hop_length - remainder
            audio = F.pad(audio, (0, pad), 'constant')
        spec = spectrogram(audio, self.n_fft, self.hop_length, self.power, self.center)
        mel_spec = self.mel_scale(spec)
        return mel_spec

class Mfcc(nn.Module):
    def __init__(self, n_fft=2048, hop_length=1024, n_mels=128, n_mfcc=40, norm='ortho', sample_rate=16000, f_min=40, f_max=7600, pad_end=True, center=False):
        """
        uses log mels
        """
        super().__init__()
        self.norm = norm
        self.n_mfcc = n_mfcc
        self.melspec = MelSpec(n_fft, hop_length, n_mels, sample_rate, power=2, f_min=f_min, f_max=f_max, pad_end=pad_end, center=center)
        dct_mat = create_dct(self.n_mfcc, self.melspec.n_mels, self.norm)
        self.register_buffer('dct_mat', dct_mat)

    def forward(self, audio):
        mel_spec = self.melspec(audio)
        mel_spec = torch.log(mel_spec+1e-6)
        # (batch, n_mels, time).tranpose(...) dot (n_mels, n_mfcc)
        # -> (batch, time, n_mfcc).tranpose(...)
        mfcc = torch.matmul(mel_spec.transpose(1, 2), self.dct_mat).transpose(1, 2)
        return mfcc

def spectrogram(audio, size=2048, hop_length=1024, power=2, center=False, window=None):
    power_spec = amp(torch.stft(audio, size, window=window, hop_length=hop_length, center=center))
    if power == 2:
        spec = power_spec
    elif power == 1:
        spec = power_spec.sqrt()
    return spec

def MultiscaleFFT(audio, sizes=[64, 128, 256, 512, 1024, 2048], overlap=0.75) -> torch.Tensor:
    """multiscale fft power spectrogram
    uses torch.stft so it should be differentiable

    Args:
        audio : (batch) input audio tensor Shape: [(batch), n_samples]
        sizes : fft sizes. Defaults to [64, 128, 256, 512, 1024, 2048].
        overlap : overlap between windows. Defaults to 0.75.
    """
    specs = []
    if isinstance(audio, np.ndarray):
        audio = torch.from_numpy(audio)
    for size in sizes:
        window = torch.hann_window(size).to(audio.device)
        stft = torch.stft(audio, size, window=window, hop_length=int((1-overlap)*size), center=False)
        specs.append(amp(stft))
    return specs

def compute_loudness(audio, sample_rate=16000, frame_rate=50, n_fft=2048, range_db=120.0, ref_db=20.7):
    """Perceptual loudness in dB, relative to white noise, amplitude=1.

    Args:
        audio: tensor. Shape [batch_size, audio_length] or [audio_length].
        sample_rate: Audio sample rate in Hz.
        frame_rate: Rate of loudness frames in Hz.
        n_fft: Fft window size.
        range_db: Sets the dynamic range of loudness in decibels. The minimum loudness (per a frequency bin) corresponds to -range_db.
        ref_db: Sets the reference maximum perceptual loudness as given by (A_weighting + 10 * log10(abs(stft(audio))**2.0). The default value corresponds to white noise with amplitude=1.0 and n_fft=2048. There is a slight dependence on fft_size due to different granularity of perceptual weighting.

    Returns:
        Loudness in decibels. Shape [batch_size, n_frames] or [n_frames,].
    """
    # Temporarily a batch dimension for single examples.
    is_1d = (len(audio.shape) == 1)
    if is_1d:
        audio = audio[None, :]

    # Take STFT.
    hop_length = sample_rate // frame_rate
    s = torch.stft(audio, n_fft=n_fft, hop_length=hop_length)
    # batch, frequency_bins, n_frames

    # Compute power of each bin
    amplitude = torch.sqrt(amp(s) + 1e-5) #sqrt(0) gives nan gradient
    power_db = torch.log10(amplitude + 1e-5)
    power_db *= 20.0

    # Perceptual weighting.
    frequencies = librosa.fft_frequencies(sr=sample_rate, n_fft=n_fft)
    a_weighting = librosa.A_weighting(frequencies)[None, :, None]
    loudness = power_db + torch.from_numpy(a_weighting.astype(np.float32)).to(audio.device)

    # Set dynamic range.
    loudness -= ref_db
    loudness = torch.clamp(loudness, min=-range_db)

    # Average over frequency bins.
    loudness = torch.mean(loudness, dim=1)

    # Remove temporary batch dimension.
    loudness = loudness[0] if is_1d else loudness

    # Compute expected length of loudness vector
    n_secs = audio.shape[-1] / float(sample_rate)  # `n_secs` can have milliseconds
    expected_len = int(n_secs * frame_rate)

    # Pad with `-range_db` noise floor or trim vector
    loudness = pad_or_trim_to_expected_length(loudness, expected_len, -range_db)
    return loudness

def compute_f0(audio, sample_rate, frame_rate, viterbi=True):
    """Fundamental frequency (f0) estimate using CREPE.

    This function is non-differentiable and takes input as a numpy array.
    Args:
        audio: Numpy ndarray of single audio example. Shape [audio_length,].
        sample_rate: Sample rate in Hz.
        frame_rate: Rate of f0 frames in Hz.
        viterbi: Use Viterbi decoding to estimate f0.

    Returns:
        f0_hz: Fundamental frequency in Hz. Shape [n_frames,].
    """

    n_secs = len(audio) / float(sample_rate)  # `n_secs` can have milliseconds
    crepe_step_size = 1000 / frame_rate  # milliseconds
    expected_len = int(n_secs * frame_rate)
    audio = np.asarray(audio)

    # Compute f0 with crepe.
    _, f0_hz, f0_confidence, _ = crepe.predict(audio, sr=sample_rate, viterbi=viterbi, step_size=crepe_step_size, center=False, verbose=0)

    # Postprocessing on f0_hz
    f0_hz = pad_or_trim_to_expected_length(torch.from_numpy(f0_hz), expected_len, 0)  # pad with 0
    f0_hz = f0_hz.numpy().astype(np.float32)

    # # Postprocessing on f0_confidence
    # f0_confidence = pad_or_trim_to_expected_length(f0_confidence, expected_len, 1)
    # f0_confidence = np.nan_to_num(f0_confidence)   # Set nans to 0 in confidence
    # f0_confidence = f0_confidence.astype(np.float32)
    return f0_hz

class SpectralLoss(nn.Module):
    def __init__(self, fft_sizes=[64, 128, 256, 512, 1024, 2048], overlap=0.75, sample_rate=16000, mag_w=1.0, log_mag_w=1.0, loud_w=0.0):
        super().__init__()
        self.fft_sizes = fft_sizes
        self.overlap = overlap
        self.mag_w = mag_w
        self.log_mag_w = log_mag_w
        self.loud_w = loud_w
        self.sample_rate = sample_rate# only needed for loudness

    def loss_func(self, input, target, loss_type, reduction='mean'):
        if loss_type == 'L1':
            return F.l1_loss(input, target, reduction='mean')
        if loss_type == 'MSE':
            return F.mse_loss(input, target, reduction='mean')
        if loss_type == 'smooth_L1':
            return F.smooth_l1_loss(input, target, reduction='mean')
    
    def forward(self, input_audio, target_audio, loss_type='L1', reduction='mean'):
        """get loss of input audio

        Args:
            input_audio (torch.Tensor): Shape [batch, n_samples]
            target (torch.Tensor): target waveform, Shape [batch, n_samples]
            loss_type (str, optional): Loss function for comparing spectral feature. Defaults to 'L1'.
            reduction (str, optional): Reduce by mean/sum or None. Defaults to 'mean'.
        """
        specs = MultiscaleFFT(input_audio, self.fft_sizes, self.overlap)
        target_specs = MultiscaleFFT(target_audio, self.fft_sizes, self.overlap)
        
        batch_size = input_audio.shape[0]
        loss = 0.0
        for i, spec in enumerate(specs):
            target_spec = target_specs[i]
            if self.mag_w > 0:
                loss += self.mag_w * self.loss_func(spec, target_spec, loss_type, reduction)
            if self.log_mag_w > 0:
                loss += self.log_mag_w * self.loss_func(log_eps(spec), log_eps(target_spec), loss_type, reduction)
        
        if self.loud_w > 0: # don't use this
            input_l = compute_loudness(input_audio, self.sample_rate)
            target_l = compute_loudness(target_audio, self.sample_rate)
            loss += self.loud_w * self.loss_func(input_l, target_l, loss_type, reduction)
        
        return loss

def loudness_loss(input_audio, target_audio, sr=16000):
    input_l = compute_loudness(input_audio, sr)
    target_l = compute_loudness(target_audio, sr)
    return F.l1_loss(input_l, target_l, reduction='mean')