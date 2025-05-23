import json
import os
import re
import shutil
from concurrent.futures import Future
from pathlib import Path
from typing import Any, List, Optional

import numpy as np
import torch
from safetensors.torch import load_file as load_safetensors_file
from transformers import AutoTokenizer

from .model import ModelArgs
from .world import World


def seed_everything(seed: int) -> None:
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # for multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


_DEVICE = None


def setup_device(device: str, world: World) -> torch.device:
    global _DEVICE
    if _DEVICE is None:
        if device == "cuda":
            _DEVICE = torch.device(f"cuda:{world.local_rank}")
        elif device == "cpu":
            _DEVICE = torch.device("cpu")
        else:
            raise NotImplementedError(f"Device {device} not implemented")
    return _DEVICE


def get_device() -> torch.device:
    global _DEVICE
    if _DEVICE is None:
        raise ValueError("Device not set")
    return _DEVICE


def get_precision(precision: str) -> torch.dtype:
    if precision == "float16":
        return torch.float16
    elif precision == "bfloat16":
        return torch.bfloat16
    else:
        raise NotImplementedError(f"Precision {precision} not implemented")


def get_tokenizer(model_name):
    return AutoTokenizer.from_pretrained(model_name)


def to_int_or_none(value: str) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def fake_future(result: Any) -> Future:
    future = Future()
    future.set_result(result)
    return future


def mean(values: List[float]) -> float:
    return np.mean(values) if len(values) > 0 else 0


def flatten_list(list_of_lists: List[List[Any]]) -> List[Any]:
    """Flatten a list of lists into a single list."""
    return [item for sublist in list_of_lists for item in sublist]


def discard_initial_tokens(decoded_tokens: List, num_discard_tokens: int) -> List:
    """Discard the initial tokens from the decoded tokens."""
    return decoded_tokens[num_discard_tokens:]


def convert_model(model_name: str) -> None:
    config = ModelArgs.from_name(model_name)

    # Load the json file containing weight mapping
    cache_dir = os.environ.get("CACHE_DIR", Path.cwd() / "checkpoints")
    checkpoint_dir = Path(cache_dir) / model_name
    model_map_json_safetensors = checkpoint_dir / "model.safetensors.index.json"
    model_map_json_pytorch = checkpoint_dir / "pytorch_model.bin.index.json"
    model_map_json = None

    try:
        assert model_map_json_safetensors.is_file()
        model_map_json = model_map_json_safetensors
    except AssertionError:
        pass
    if model_map_json is None:
        try:
            assert model_map_json_pytorch.is_file()
            model_map_json = model_map_json_pytorch
            print(f"Found pytorch index at {model_map_json_pytorch}")
        except AssertionError:
            print(f"{model_map_json_pytorch} not found")

    if model_map_json is None:
        raise Exception("No model map found!")

    with open(model_map_json) as json_map:
        bin_index = json.load(json_map)

    weight_map = {
        "model.embed_tokens.weight": "tok_embeddings.weight",
        "model.layers.{}.self_attn.q_proj.weight": "layers.{}.attention.wq.weight",
        "model.layers.{}.self_attn.k_proj.weight": "layers.{}.attention.wk.weight",
        "model.layers.{}.self_attn.v_proj.weight": "layers.{}.attention.wv.weight",
        "model.layers.{}.self_attn.o_proj.weight": "layers.{}.attention.wo.weight",
        "model.layers.{}.self_attn.rotary_emb.inv_freq": None,
        "model.layers.{}.mlp.gate_proj.weight": "layers.{}.feed_forward.w1.weight",
        "model.layers.{}.mlp.up_proj.weight": "layers.{}.feed_forward.w3.weight",
        "model.layers.{}.mlp.down_proj.weight": "layers.{}.feed_forward.w2.weight",
        "model.layers.{}.input_layernorm.weight": "layers.{}.attention_norm.weight",
        "model.layers.{}.post_attention_layernorm.weight": "layers.{}.ffn_norm.weight",
        "model.norm.weight": "norm.weight",
        "lm_head.weight": "output.weight",
    }
    bin_files = {checkpoint_dir / bin for bin in bin_index["weight_map"].values()}

    def permute(w, n_head):
        dim = config.dim
        return w.view(n_head, 2, config.head_dim // 2, dim).transpose(1, 2).reshape(config.head_dim * n_head, dim)

    merged_result = {}
    for file in sorted(bin_files):
        if "safetensors" in str(file):
            state_dict = load_safetensors_file(str(file), device="cpu")
            merged_result.update(state_dict)
        else:
            state_dict = torch.load(str(file), map_location="cpu", mmap=True, weights_only=True)
            merged_result.update(state_dict)
    final_result = {}
    for key, value in merged_result.items():
        if "layers" in key:
            abstract_key = re.sub(r"(\d+)", "{}", key)
            layer_num = re.search(r"\d+", key).group(0)
            new_key = weight_map[abstract_key]
            if new_key is None:
                continue
            new_key = new_key.format(layer_num)
        else:
            new_key = weight_map[key]

        final_result[new_key] = value

    for key in tuple(final_result.keys()):
        if "wq" in key:
            q = final_result[key]
            k = final_result[key.replace("wq", "wk")]
            v = final_result[key.replace("wq", "wv")]
            q = permute(q, config.n_head)
            k = permute(k, config.n_local_heads)
            final_result[key.replace("wq", "wqkv")] = torch.cat([q, k, v])
            del final_result[key]
            del final_result[key.replace("wq", "wk")]
            del final_result[key.replace("wq", "wv")]
    torch.save(final_result, checkpoint_dir / "model.pth")
    if "llama-3-" in model_name.lower() or "llama-3.1-" in model_name.lower():
        if "llama-3.1-405b" in model_name.lower():
            original_dir = checkpoint_dir / "original" / "mp16"
        else:
            original_dir = checkpoint_dir / "original"
        tokenizer_model = original_dir / "tokenizer.model"
        tokenizer_model_tiktoken = checkpoint_dir / "tokenizer.model"
        shutil.copy(tokenizer_model, tokenizer_model_tiktoken)
