import math
import torch
from torch import nn as nn
from torch.nn import functional as F
from timm.models.vision_transformer import DropPath, Mlp, Attention
from timm.models.layers import trunc_normal_

from layers import FusionBlock, get_projection, GAP
from annotated_transformer import Encoder, Decoder, LayerNorm
from annotated_transformer import MultiHeadedAttention, PositionwiseFeedForward

from detr_transformer import TransformerEncoder, TransformerEncoderLayer

class ShallowNN(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.Sigmoid, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act1 = nn.GELU()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.act2 = act_layer()
        self.drop = nn.Dropout(drop)
        self.fc1.weight.data.normal_(0, math.sqrt(1. / hidden_features))
        self.fc2.weight.data.normal_(0, math.sqrt(1. / out_features))

    def forward(self, x):
        x = self.fc1(x)
        x = self.act1(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.act2(x)
        return x

class FeatureFusion(nn.Module):
    def __init__(self, input_dimension, hidden_features=256, out_features=1):
        super().__init__()
        self.fc = ShallowNN(input_dimension, hidden_features=hidden_features, out_features=out_features)

    def forward(self, feature_audio, feature_video, mask):
        feature_audio, feature_video, mask = feature_audio.sum(dim=1), feature_video.sum(dim=1), mask.sum(dim=1)
        feature_audio = torch.div(feature_audio, mask.unsqueeze(1))
        feature_video = torch.div(feature_video, mask.unsqueeze(1))
        x = torch.cat((feature_audio, feature_video), dim=1)
        x = self.fc(x).squeeze(-1)
        return x

class TransformerFusion(nn.Module):
    def __init__(self, video_dimension, audio_dimension, fused_dimension):
        super().__init__()
        self.audio = FusionBlock(audio_dimension, 1, mlp_ratio=1)
        self.video = FusionBlock(video_dimension, 1, mlp_ratio=1)
        #self.fused = FusionBlock(fused_dimension, 1)
        self.fc = ShallowNN(161, hidden_features=128, out_features=1)
    
    def forward(self, feature_audio, feature_video, mask):
        feature_audio = self.audio(feature_audio, mask)
        feature_video = self.video(feature_video, mask)
        feature_audio, feature_video, mask = feature_audio.sum(dim=1), feature_video.sum(dim=1), mask.sum(dim=1)
        feature_audio = torch.div(feature_audio, mask.unsqueeze(1))
        feature_video = torch.div(feature_video, mask.unsqueeze(1))
        x = torch.cat((feature_audio, feature_video), dim=1)
        x = self.fc(x).squeeze(-1)

        return x

class StanfordTransformerFusion(nn.Module):
    def __init__(self, video_dimension, audio_dimension, fused_dimension):
        super().__init__()
        feed_forward = 256
        dropout = 0.1
        num_layers = 1
        num_heads = 1

        self.audio_prj = get_projection(audio_dimension, fused_dimension, 'minimal')
        self.video_prj = get_projection(video_dimension, fused_dimension, 'minimal')
        self.audio_enc = Encoder(fused_dimension, num_heads, feed_forward, dropout, num_layers)
        self.video_enc = Encoder(fused_dimension, num_heads, feed_forward, dropout, num_layers)
        self.fused_dec = Decoder(fused_dimension, num_heads, feed_forward, dropout)
        self.gap = GAP()
        self.mlp = ShallowNN(fused_dimension*2, hidden_features=fused_dimension, out_features=1, drop=dropout)
    
    def forward(self, a, v, m):
        a = self.audio_prj(a)
        v = self.video_prj(v)
        a = self.audio_enc(a, m)
        v = self.video_enc(v, m)
        f = self.fused_dec(a, v, m)
        out = self.gap(f, m)
        out = self.mlp(out).squeeze(-1)

        return out




'''
    For Ablation Study
'''
class MaskedPassThrough(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x, m=None):
        return x

class PassThroughAttention(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, q, k, v, m=None):
        return q

class AblationModel(nn.Module):
    def __init__(self, video_dimension, audio_dimension, fused_dimension, config_num, project_type='conv1d', pre_norm=False):
        super().__init__()
        feed_forward = 256
        dropout = 0.1
        num_layers = 4
        num_heads = 4

        self.pre_norm = pre_norm
        self.project_type_conv1d = (project_type == 'conv1d')
        self.audio_prj = get_projection(audio_dimension, fused_dimension, project_type)
        self.video_prj = get_projection(video_dimension, fused_dimension, project_type)

        self.enable_self_attention = config_num % 2
        self.enable_cross_attention = (config_num // 2) % 2
        self.enable_fused_attention = (config_num // 4) % 2

        self.cls_token = nn.Parameter(torch.zeros(1, 1, fused_dimension*2))
        self.mask_appnd = nn.Parameter(torch.ones(1, 1))
        trunc_normal_(self.cls_token, std=.02)

        norms = []
        norms.append(LayerNorm(fused_dimension))
        norms.append(LayerNorm(fused_dimension))
        norms.append(LayerNorm(fused_dimension))
        norms.append(LayerNorm(fused_dimension))
        norms.append(LayerNorm(fused_dimension))
        norms.append(LayerNorm(fused_dimension))
        norms.append(LayerNorm(fused_dimension*2))
        norms.append(LayerNorm(fused_dimension*2))
        self.norms = nn.ModuleList(norms)

        self.aself_attn = MultiHeadedAttention(num_heads, fused_dimension)
        self.afeed_forward = PositionwiseFeedForward(fused_dimension, feed_forward, dropout)
        self.adrop1 = nn.Dropout(dropout)
        self.adrop2 = nn.Dropout(dropout)

        self.vself_attn = MultiHeadedAttention(num_heads, fused_dimension)
        self.vfeed_forward = PositionwiseFeedForward(fused_dimension, feed_forward, dropout)
        self.vdrop1 = nn.Dropout(dropout)
        self.vdrop2 = nn.Dropout(dropout)

        self.audio_attn = MultiHeadedAttention(num_heads, fused_dimension)
        self.video_attn = MultiHeadedAttention(num_heads, fused_dimension)
        self.drop1 = nn.Dropout(dropout)
        self.drop2 = nn.Dropout(dropout)

        self.fused_attn = MultiHeadedAttention(num_heads, fused_dimension*2)
        self.feed_forward = PositionwiseFeedForward(fused_dimension*2, feed_forward, dropout)
        self.drop3 = nn.Dropout(dropout)
        self.drop4 = nn.Dropout(dropout)

        self.head = nn.Linear(fused_dimension*2, 2)


    def maybe_normalize(self, i, x, pre):
        if self.pre_norm == pre:
            return  self.norms[i](x)

        return x

    def forward(self, a, v, m):
        B = a.shape[0]
        if self.project_type_conv1d:
            a = self.audio_prj(a.transpose(1, 2)).transpose(1, 2)
            v = self.video_prj(v.transpose(1, 2)).transpose(1, 2)
        else:
            a = self.audio_prj(a)
            v = self.video_prj(v)

        if self.enable_self_attention:
            residual = a
            a = self.maybe_normalize(0, a, pre=True)
            a = residual + self.adrop1(self.aself_attn(a, a, a, m))
            a = self.maybe_normalize(0, a, pre=False)

            residual = a
            a = self.maybe_normalize(1, a, pre=True)
            a = residual + self.adrop2(self.afeed_forward(a))
            a = self.maybe_normalize(1, a, pre=False)

            residual = v
            v = self.maybe_normalize(2, v, pre=True)
            v = residual + self.vdrop1(self.vself_attn(v, v, v, m))
            v = self.maybe_normalize(2, v, pre=False)

            residual = v
            v = self.maybe_normalize(3, v, pre=True)
            v = residual + self.vdrop2(self.vfeed_forward(v))
            v = self.maybe_normalize(3, v, pre=False)

        if self.enable_cross_attention:
            residual_a = a
            residual_v = v
            a = self.maybe_normalize(4, a, pre=True)
            v = self.maybe_normalize(5, v, pre=True)
            a = residual_a + self.drop1(self.audio_attn(a, v, v, m))
            v = residual_v + self.drop2(self.video_attn(v, a, a, m))
            a = self.maybe_normalize(4, a, pre=False)
            v = self.maybe_normalize(5, v, pre=False)

        f = torch.cat((a, v), dim=2)
        cls_tokens = self.cls_token.expand(B, -1, -1)  # stole cls_tokens impl from Phil Wang, thanks
        mask_appnd = self.mask_appnd.expand(B, -1)
        m = torch.cat((mask_appnd, m), dim=1)
        f = torch.cat((cls_tokens, f), dim=1)

        residual_f = f
        f = self.maybe_normalize(6, f, pre=True)
        f = residual_f + self.drop3(self.fused_attn(f, f, f, m))
        f = self.maybe_normalize(6, f, pre=False)
        residual_f = f
        f = self.maybe_normalize(7, f, pre=True)
        f = residual_f + self.drop4(self.feed_forward(f))
        f = self.maybe_normalize(7, f, pre=False)

        out = f[:, 0]
        out = self.head(out)

        return out


class DetrTransformerFusion(nn.Module):
    def __init__(self, video_dimension, audio_dimension, fused_dimension):
        super().__init__()
        feed_forward = 256
        dropout = 0.2
        num_layers = 1
        num_heads = 1
        normalize_before = True

        self.audio_prj = get_projection(audio_dimension, fused_dimension, 'minimal')
        self.video_prj = get_projection(video_dimension, fused_dimension, 'minimal')

        
        a_encoder_layer = TransformerEncoderLayer(fused_dimension, num_heads, feed_forward, dropout, "relu", normalize_before)
        a_encoder_norm = nn.LayerNorm(fused_dimension) if normalize_before else None
        self.audio_enc = TransformerEncoder(a_encoder_layer, num_layers, a_encoder_norm)

        v_encoder_layer = TransformerEncoderLayer(fused_dimension, num_heads, feed_forward, dropout, "relu", normalize_before)
        v_encoder_norm = nn.LayerNorm(fused_dimension) if normalize_before else None
        self.video_enc = TransformerEncoder(v_encoder_layer, num_layers, v_encoder_norm)

        f_encoder_layer = TransformerEncoderLayer(fused_dimension*2, num_heads, feed_forward, dropout, "relu", normalize_before)
        f_encoder_norm = nn.LayerNorm(fused_dimension*2) if normalize_before else None
        self.fused_enc = TransformerEncoder(f_encoder_layer, num_layers, f_encoder_norm)

        self.gap = GAP()
        self.mlp = ShallowNN(fused_dimension*2, hidden_features=fused_dimension, out_features=1, drop=dropout)
    
    def forward(self, a, v, m):
        mask = (m == 0).long()
        a = self.audio_prj(a)
        v = self.video_prj(v)
        a = a.permute(1, 0, 2)
        v = v.permute(1, 0, 2)
        a = self.audio_enc(a, src_key_padding_mask=mask)
        v = self.video_enc(v, src_key_padding_mask=mask)
        f = torch.cat((a, v), dim=2)
        f = self.fused_enc(f, src_key_padding_mask=mask)
        f = f.permute(1, 0, 2)
        out = self.gap(f, m)
        out = self.mlp(out).squeeze(-1)

        return out


