"""A deterministic, dependency-free UTF-8 byte tokenizer.

Byte tokens keep the vocabulary fixed, so adding approved training data never
requires rewriting embeddings or losing older token IDs.
"""

import codecs


class ByteTokenizer:
    bos_id = 256
    eos_id = 257
    vocab_size = 258

    def encode(self, text: str, add_special_tokens: bool = True) -> list[int]:
        tokens = list(text.encode("utf-8", errors="replace"))
        return [self.bos_id, *tokens, self.eos_id] if add_special_tokens else tokens

    def decode(self, tokens: list[int], errors: str = "replace") -> str:
        raw = bytes(token for token in tokens if 0 <= token <= 255)
        return raw.decode("utf-8", errors=errors)

    def stream_decoder(self):
        return codecs.getincrementaldecoder("utf-8")(errors="replace")
