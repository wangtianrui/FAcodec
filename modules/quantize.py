from dac.nn.quantize import ResidualVectorQuantize
from torch import nn
from modules.wavenet import WN
from modules.style_encoder import StyleEncoder
from gradient_reversal import GradientReversal
import torch
import torchaudio.functional as audio_F
import numpy as np
from alias_free_torch import *
from torch.nn.utils import weight_norm
from torch import nn, sin, pow
from einops.layers.torch import Rearrange

def init_weights(m):
    if isinstance(m, nn.Conv1d):
        nn.init.trunc_normal_(m.weight, std=0.02)
        nn.init.constant_(m.bias, 0)


def WNConv1d(*args, **kwargs):
    return weight_norm(nn.Conv1d(*args, **kwargs))


def WNConvTranspose1d(*args, **kwargs):
    return weight_norm(nn.ConvTranspose1d(*args, **kwargs))

class SnakeBeta(nn.Module):
    """
    A modified Snake function which uses separate parameters for the magnitude of the periodic components
    Shape:
        - Input: (B, C, T)
        - Output: (B, C, T), same shape as the input
    Parameters:
        - alpha - trainable parameter that controls frequency
        - beta - trainable parameter that controls magnitude
    References:
        - This activation function is a modified version based on this paper by Liu Ziyin, Tilman Hartwig, Masahito Ueda:
        https://arxiv.org/abs/2006.08195
    Examples:
        >>> a1 = snakebeta(256)
        >>> x = torch.randn(256)
        >>> x = a1(x)
    """

    def __init__(
        self, in_features, alpha=1.0, alpha_trainable=True, alpha_logscale=False
    ):
        """
        Initialization.
        INPUT:
            - in_features: shape of the input
            - alpha - trainable parameter that controls frequency
            - beta - trainable parameter that controls magnitude
            alpha is initialized to 1 by default, higher values = higher-frequency.
            beta is initialized to 1 by default, higher values = higher-magnitude.
            alpha will be trained along with the rest of your model.
        """
        super(SnakeBeta, self).__init__()
        self.in_features = in_features

        # initialize alpha
        self.alpha_logscale = alpha_logscale
        if self.alpha_logscale:  # log scale alphas initialized to zeros
            self.alpha = nn.Parameter(torch.zeros(in_features) * alpha)
            self.beta = nn.Parameter(torch.zeros(in_features) * alpha)
        else:  # linear scale alphas initialized to ones
            self.alpha = nn.Parameter(torch.ones(in_features) * alpha)
            self.beta = nn.Parameter(torch.ones(in_features) * alpha)

        self.alpha.requires_grad = alpha_trainable
        self.beta.requires_grad = alpha_trainable

        self.no_div_by_zero = 0.000000001

    def forward(self, x):
        """
        Forward pass of the function.
        Applies the function to the input elementwise.
        SnakeBeta := x + 1/b * sin^2 (xa)
        """
        alpha = self.alpha.unsqueeze(0).unsqueeze(-1)  # line up with x to [B, C, T]
        beta = self.beta.unsqueeze(0).unsqueeze(-1)
        if self.alpha_logscale:
            alpha = torch.exp(alpha)
            beta = torch.exp(beta)
        x = x + (1.0 / (beta + self.no_div_by_zero)) * pow(sin(x * alpha), 2)

        return x

class ResidualUnit(nn.Module):
    def __init__(self, dim: int = 16, dilation: int = 1):
        super().__init__()
        pad = ((7 - 1) * dilation) // 2
        self.block = nn.Sequential(
            Activation1d(activation=SnakeBeta(dim, alpha_logscale=True)),
            WNConv1d(dim, dim, kernel_size=7, dilation=dilation, padding=pad),
            Activation1d(activation=SnakeBeta(dim, alpha_logscale=True)),
            WNConv1d(dim, dim, kernel_size=1),
        )

    def forward(self, x):
        return x + self.block(x)

class CNNLSTM(nn.Module):
    def __init__(self, indim, outdim, head, global_pred=False):
        super().__init__()
        self.global_pred = global_pred
        self.model = nn.Sequential(
            ResidualUnit(indim, dilation=1),
            ResidualUnit(indim, dilation=2),
            ResidualUnit(indim, dilation=3),
            Activation1d(activation=SnakeBeta(indim, alpha_logscale=True)),
            Rearrange("b c t -> b t c"),
        )
        self.heads = nn.ModuleList([nn.Linear(indim, outdim) for i in range(head)])

    def forward(self, x):
        # x: [B, C, T]
        x = self.model(x)
        if self.global_pred:
            x = torch.mean(x, dim=1, keepdim=False)
        outs = [head(x) for head in self.heads]
        return outs

def sequence_mask(length, max_length=None):
  if max_length is None:
    max_length = length.max()
  x = torch.arange(max_length, dtype=length.dtype, device=length.device)
  return x.unsqueeze(0) < length.unsqueeze(1)

class MFCC(nn.Module):
    def __init__(self, n_mfcc=40, n_mels=80):
        super(MFCC, self).__init__()
        self.n_mfcc = n_mfcc
        self.n_mels = n_mels
        self.norm = 'ortho'
        dct_mat = audio_F.create_dct(self.n_mfcc, self.n_mels, self.norm)
        self.register_buffer('dct_mat', dct_mat)

    def forward(self, mel_specgram):
        if len(mel_specgram.shape) == 2:
            mel_specgram = mel_specgram.unsqueeze(0)
            unsqueezed = True
        else:
            unsqueezed = False
        # (channel, n_mels, time).tranpose(...) dot (n_mels, n_mfcc)
        # -> (channel, time, n_mfcc).tranpose(...)
        mfcc = torch.matmul(mel_specgram.transpose(1, 2), self.dct_mat).transpose(1, 2)

        # unpack batch
        if unsqueezed:
            mfcc = mfcc.squeeze(0)
        return mfcc
class FAquantizer(nn.Module):
    def __init__(self, in_dim=1024,
                 n_p_codebooks=1,
                 n_c_codebooks=2,
                 n_r_codebooks=3,
                 codebook_size=1024,
                 codebook_dim=8,
                 quantizer_dropout=0.5):
        super(FAquantizer, self).__init__()
        self.prosody_quantizer = ResidualVectorQuantize(
            input_dim=in_dim,
            n_codebooks=n_p_codebooks,
            codebook_size=codebook_size,
            codebook_dim=codebook_dim,
            quantizer_dropout=quantizer_dropout,
        )

        self.content_quantizer = ResidualVectorQuantize(
            input_dim=in_dim,
            n_codebooks=n_c_codebooks,
            codebook_size=codebook_size,
            codebook_dim=codebook_dim,
            quantizer_dropout=quantizer_dropout,
        )

        self.residual_quantizer = ResidualVectorQuantize(
            input_dim=in_dim,
            n_codebooks=n_r_codebooks,
            codebook_size=codebook_size,
            codebook_dim=codebook_dim,
            quantizer_dropout=quantizer_dropout,
        )

        self.melspec_linear = nn.Conv1d(20, 256, 1)
        self.melspec_encoder = WN(hidden_channels=256, kernel_size=5, dilation_rate=1, n_layers=8, gin_channels=0, p_dropout=0.2)
        self.melspec_linear2 = nn.Conv1d(256, 1024, 1)

        self.content_linear = nn.Conv1d(40, 256, 1)
        self.content_encoder = WN(hidden_channels=256, kernel_size=5, dilation_rate=1, n_layers=16, gin_channels=0, p_dropout=0.2)
        self.content_linear2 = nn.Conv1d(256, 1024, 1)
        self.to_mfcc = MFCC(n_mfcc=40, n_mels=80)

        self.timbre_encoder = StyleEncoder(in_dim=80, hidden_dim=256, out_dim=1024)
        self.timbre_linear = nn.Linear(1024, 1024 * 2)
        self.timbre_linear.bias.data[:1024] = 1
        self.timbre_linear.bias.data[1024:] = 0
        self.timbre_norm = nn.LayerNorm(1024, elementwise_affine=False)

        self.prob_random_mask_residual = 0.75

    def forward(self, x, prosody_feature, mel_segments, mels, mel_lens):
        # timbre = self.timbre_encoder(mels, sequence_mask(mel_lens, mels.size(-1)).unsqueeze(1))
        timbre = self.timbre_encoder(mel_segments, torch.ones(mel_segments.size(0), 1, mel_segments.size(2)).bool().to(mel_segments.device))

        outs = 0

        f0_input = prosody_feature # (B, T, 20)
        f0_input = self.melspec_linear(f0_input)
        f0_input = self.melspec_encoder(f0_input, torch.ones(f0_input.shape[0], 1, f0_input.shape[2]).to(f0_input.device).bool())
        f0_input = self.melspec_linear2(f0_input)

        content_input = self.to_mfcc(mel_segments)
        content_input = self.content_linear(content_input)
        content_input = self.content_encoder(content_input, torch.ones(content_input.shape[0], 1, content_input.shape[2]).to(content_input.device).bool())
        content_input = self.content_linear2(content_input)

        z_p, codes_p, latents_p, commitment_loss_p, codebook_loss_p = self.prosody_quantizer(
            f0_input, 1
        )
        outs += z_p.detach()

        z_c, codes_c, latents_c, commitment_loss_c, codebook_loss_c = self.content_quantizer(
            content_input, 2
        )
        outs += z_c.detach()

        residual_feature = x - z_p.detach() - z_c.detach()

        z_r, codes_r, latents_r, commitment_loss_r, codebook_loss_r = self.residual_quantizer(
            residual_feature, 3
        )

        bsz = z_r.shape[0]
        res_mask = np.random.choice(
            [0, 1],
            size=bsz,
            p=[
                self.prob_random_mask_residual,
                1 - self.prob_random_mask_residual,
            ],
        )
        res_mask = (
            torch.from_numpy(res_mask).unsqueeze(1).unsqueeze(1)
        )  # (B, 1, 1)
        res_mask = res_mask.to(
            device=z_r.device, dtype=z_r.dtype
        )

        outs += z_r * res_mask

        quantized = [z_p, z_c, z_r]
        commitment_losses = commitment_loss_p + commitment_loss_c + commitment_loss_r
        codebook_losses = codebook_loss_p + codebook_loss_c + codebook_loss_r

        style = self.timbre_linear(timbre).unsqueeze(2)  # (B, 2d, 1)
        gamma, beta = style.chunk(2, 1)  # (B, d, 1)
        outs = outs.transpose(1, 2)
        outs = self.timbre_norm(outs)
        outs = outs.transpose(1, 2)
        outs = outs * gamma + beta
        return outs, quantized, commitment_losses, codebook_losses, timbre

class FApredictors(nn.Module):
    def __init__(self,
                 in_dim=1024,
                 use_gr_content_f0=False,
                 use_gr_prosody_phone=False,
                 use_gr_residual_f0=False,
                 use_gr_residual_phone=False,
                 use_gr_x_timbre=False,
                 ):
        super(FApredictors, self).__init__()
        self.f0_predictor = CNNLSTM(in_dim, 1, 2)
        self.phone_predictor = CNNLSTM(in_dim, 1024, 1)
        self.timbre_predictor = nn.Linear(in_dim, 114514)

        self.use_gr_content_f0 = use_gr_content_f0
        self.use_gr_prosody_phone = use_gr_prosody_phone
        self.use_gr_residual_f0 = use_gr_residual_f0
        self.use_gr_residual_phone = use_gr_residual_phone
        self.use_gr_x_timbre = use_gr_x_timbre

        if self.use_gr_residual_f0:
            self.res_f0_predictor = nn.Sequential(
                GradientReversal(alpha=1.0), CNNLSTM(in_dim, 1, 2)
            )

        if self.use_gr_residual_phone > 0:
            self.res_phone_predictor = nn.Sequential(
                GradientReversal(alpha=1.0), CNNLSTM(in_dim, 1024, 1)
            )

        if self.use_gr_content_f0:
            self.content_f0_predictor = nn.Sequential(
                GradientReversal(alpha=1.0), CNNLSTM(in_dim, 1, 2)
            )

        if self.use_gr_prosody_phone:
            self.prosody_phone_predictor = nn.Sequential(
                GradientReversal(alpha=1.0), CNNLSTM(in_dim, 1024, 1)
            )

        if self.use_gr_x_timbre:
            self.x_timbre_predictor = nn.Sequential(
                GradientReversal(alpha=1.0),
                CNNLSTM(in_dim, 114514, 1, global_pred=True),
            )
    def forward(self, quantized, timbre):
        prosody_latent = quantized[0]
        content_latent = quantized[1]
        residual_latent = quantized[2]
        f0_pred, uv_pred = self.f0_predictor(prosody_latent)
        content_pred = self.phone_predictor(content_latent)[0]
        spk_pred = self.timbre_predictor(timbre)

        if self.use_gr_content_f0:
            content_f0_pred, content_uv_pred = self.content_f0_predictor(content_latent)
        else:
            content_f0_pred = None
            content_uv_pred = None

        if self.use_gr_prosody_phone:
            prosody_content_pred = self.prosody_phone_predictor(prosody_latent)[0]
        else:
            prosody_content_pred = None

        if self.use_gr_residual_f0:
            res_f0_pred, res_uv_pred = self.res_f0_predictor(residual_latent)
        else:
            res_f0_pred = None
            res_uv_pred = None

        if self.use_gr_residual_phone:
            res_content_pred = self.res_phone_predictor(residual_latent)[0]
        else:
            res_content_pred = None

        x = quantized[0] + quantized[1] + quantized[2]
        if self.use_gr_x_timbre:
            x_spk_pred = self.x_timbre_predictor(x)[0]
        else:
            x_spk_pred = None

        preds = {
            'f0': f0_pred,
            'uv': uv_pred,
            'content': content_pred,
            'timbre': spk_pred,
        }
        rev_preds = {
            'content_f0': content_f0_pred,
            'content_uv': content_uv_pred,
            'prosody_content': prosody_content_pred,
            'res_f0': res_f0_pred,
            'res_uv': res_uv_pred,
            'res_content': res_content_pred,
            'x_timbre': x_spk_pred,
        }
        return preds, rev_preds
