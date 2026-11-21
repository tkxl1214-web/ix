"""Small causal Transformer language model implemented directly in PyTorch."""

import math

import torch
from torch import nn
from torch.nn import functional as F


class PositionalEncoding(nn.Module):
    def __init__(self, width: int, max_length: int):
        super().__init__()
        positions = torch.arange(max_length, dtype=torch.float32).unsqueeze(1)
        scales = torch.exp(torch.arange(0, width, 2, dtype=torch.float32) * (-math.log(10_000.0) / width))
        encoding = torch.zeros(max_length, width)
        encoding[:, 0::2] = torch.sin(positions * scales)
        encoding[:, 1::2] = torch.cos(positions * scales[: encoding[:, 1::2].shape[1]])
        self.register_buffer("encoding", encoding.unsqueeze(0), persistent=True)

    def forward(self, hidden: torch.Tensor) -> torch.Tensor:
        return hidden + self.encoding[:, : hidden.size(1)].to(dtype=hidden.dtype)


class CausalSelfAttention(nn.Module):
    def __init__(self, width: int, heads: int, dropout: float, max_length: int):
        super().__init__()
        if width % heads:
            raise ValueError("embedding width must be divisible by attention heads")
        self.heads = heads
        self.head_width = width // heads
        self.qkv = nn.Linear(width, width * 3)
        self.projection = nn.Linear(width, width)
        self.attention_dropout = nn.Dropout(dropout)
        self.residual_dropout = nn.Dropout(dropout)
        self.register_buffer("causal_mask", torch.tril(torch.ones(max_length, max_length, dtype=torch.bool))[None, None])

    def forward(self, hidden: torch.Tensor) -> torch.Tensor:
        batch, length, width = hidden.shape
        query, key, value = self.qkv(hidden).chunk(3, dim=-1)
        shape = (batch, length, self.heads, self.head_width)
        query = query.view(shape).transpose(1, 2)
        key = key.view(shape).transpose(1, 2)
        value = value.view(shape).transpose(1, 2)
        scores = (query @ key.transpose(-2, -1)) / math.sqrt(self.head_width)
        scores = scores.masked_fill(~self.causal_mask[:, :, :length, :length], torch.finfo(scores.dtype).min)
        weights = self.attention_dropout(torch.softmax(scores, dim=-1))
        attended = (weights @ value).transpose(1, 2).contiguous().view(batch, length, width)
        return self.residual_dropout(self.projection(attended))


class FeedForward(nn.Module):
    def __init__(self, width: int, dropout: float):
        super().__init__()
        self.layers = nn.Sequential(
            nn.Linear(width, width * 4),
            nn.GELU(),
            nn.Linear(width * 4, width),
            nn.Dropout(dropout),
        )

    def forward(self, hidden: torch.Tensor) -> torch.Tensor:
        return self.layers(hidden)


class TransformerBlock(nn.Module):
    def __init__(self, width: int, heads: int, dropout: float, max_length: int):
        super().__init__()
        self.norm_attention = nn.LayerNorm(width)
        self.attention = CausalSelfAttention(width, heads, dropout, max_length)
        self.norm_feed_forward = nn.LayerNorm(width)
        self.feed_forward = FeedForward(width, dropout)

    def forward(self, hidden: torch.Tensor) -> torch.Tensor:
        hidden = hidden + self.attention(self.norm_attention(hidden))
        return hidden + self.feed_forward(self.norm_feed_forward(hidden))


class TinyTransformer(nn.Module):
    def __init__(self, config: dict):
        super().__init__()
        self.config = dict(config)
        width = int(config["n_embd"])
        self.token_embedding = nn.Embedding(int(config["vocab_size"]), width)
        self.position_encoding = PositionalEncoding(width, int(config["block_size"]))
        self.input_dropout = nn.Dropout(float(config["dropout"]))
        self.blocks = nn.ModuleList(
            TransformerBlock(width, int(config["n_head"]), float(config["dropout"]), int(config["block_size"]))
            for _ in range(int(config["n_layer"]))
        )
        self.final_norm = nn.LayerNorm(width)
        self.output = nn.Linear(width, int(config["vocab_size"]), bias=False)
        self.output.weight = self.token_embedding.weight
        self.apply(self._initialize)
        self.parameter_count = sum(parameter.numel() for parameter in self.parameters())

    @staticmethod
    def _initialize(module: nn.Module):
        if isinstance(module, nn.Linear):
            nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(self, tokens: torch.Tensor, targets: torch.Tensor | None = None):
        hidden = self.input_dropout(self.position_encoding(self.token_embedding(tokens)))
        for block in self.blocks:
            hidden = block(hidden)
        logits = self.output(self.final_norm(hidden))
        loss = None
        if targets is not None:
            loss = F.cross_entropy(logits.reshape(-1, logits.size(-1)), targets.reshape(-1))
        return logits, loss

    @torch.inference_mode()
    def generate_one(self, context: torch.Tensor, temperature: float = 0.85, top_k: int = 40) -> int:
        self.eval()
        context = context[:, -self.config["block_size"] :]
        logits, _ = self(context)
        scores = logits[:, -1, :] / max(temperature, 1e-4)
        if top_k > 0:
            top_values, _ = torch.topk(scores, min(top_k, scores.size(-1)))
            scores[scores < top_values[:, [-1]]] = -torch.inf
        return int(torch.multinomial(torch.softmax(scores, dim=-1), num_samples=1).item())
