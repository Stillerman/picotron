#VERBOSE=0 torchrun --nproc_per_node 3 generate.py --pp_size 3
import os
import argparse
import torch, torch.distributed as dist
from transformers import AutoTokenizer

from utils import set_all_seed
import parallel_context as pc
from parallel_context import setup_parallel_context
from pipeline_parallel import PipelineParallel
from distributed_primtives import communicate

def run_one_inference_step(model, batch, device) -> torch.Tensor:
    if pc.parallel_context.pp_world_size == 1:
        return model.forward(batch, device) 
    
    batch_size = batch["input_ids"].shape[0]
    seq_len = batch["input_ids"].shape[1]
    tensor_shapes = (batch_size, seq_len, model.config.hidden_size)

    # Preallocate memory for output logits.
    logits = None
    if pc.parallel_context.pp_is_last_stage:
        logits = torch.empty((batch_size, seq_len, int(model.config.vocab_size)), dtype=torch.float32, device=device)

    recv_buffer = communicate(operation="recv_forward", shapes=tensor_shapes, dtype=torch.float32)
    
    batch["hidden_states"] = None if pc.parallel_context.pp_is_first_stage else recv_buffer

    output_tensor = model.forward(batch, device)
    
    # Send output to the next stage.
    communicate(operation="send_forward", tensor=output_tensor)

    # Copy logits.
    if pc.parallel_context.pp_is_last_stage:
        logits = output_tensor

    dist.barrier()
    
    return logits

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pp_size", type=int, default=1)
    parser.add_argument("--max_tokens", type=int, default=32)
    args = parser.parse_args()
    
    local_rank, world_size  = int(os.environ["LOCAL_RANK"]), int(os.environ["WORLD_SIZE"])

    dist.init_process_group(backend="nccl")
    torch.cuda.set_device(local_rank)
    device = torch.device("cuda", local_rank)
    setup_parallel_context(tp_size=1, pp_size=args.pp_size, dp_size=1)
    set_all_seed(seed=42)
    model = PipelineParallel("HuggingFaceTB/SmolLM-360M-Instruct").to(device)

    model.eval()
    
    # Tokenize the input
    prompts = [
        "My name is",
        # "How old are you ?",
        # "What is your favorite color?",
    ]
    
    tokenizer = AutoTokenizer.from_pretrained("HuggingFaceTB/SmolLM-360M-Instruct")
    tokenizer.padding_side = "left"
    tokenizer.pad_token = tokenizer.eos_token
    
    tokenized_prompts = tokenizer(prompts, return_tensors="pt", padding=True).to(device=device)


    for _ in range(args.max_tokens):

        # Create the batch
        seq_len = tokenized_prompts["input_ids"].shape[1]
        position_index = torch.arange(seq_len).view(1, -1).to(device=device)
        
        batch_prompts = {
            "input_ids": tokenized_prompts["input_ids"],
            "target_ids": None,
            "position_index": position_index,
            "attn_mask": tokenized_prompts["attention_mask"].to(dtype=torch.bool),
            "hidden_states": None,
        }
        
        logits = run_one_inference_step(model, batch_prompts, device)

        # Sample new token
        if pc.parallel_context.pp_is_last_stage:
            assert logits is not None    
            next_token = torch.argmax(logits[:, -1], dim=-1)
            tokenized_prompts["input_ids"] = torch.cat([tokenized_prompts["input_ids"], next_token.unsqueeze(-1)], dim=-1)
            tokenized_prompts["attention_mask"] = torch.cat([tokenized_prompts["attention_mask"], torch.ones((tokenized_prompts["attention_mask"].shape[0], 1), dtype=torch.int64, device=device)], dim=-1)
        else:
            tokenized_prompts["input_ids"] = torch.zeros((tokenized_prompts["input_ids"].shape[0], tokenized_prompts["input_ids"].shape[1] + 1), dtype=torch.int64, device=device)
            tokenized_prompts["attention_mask"] = torch.zeros((tokenized_prompts["attention_mask"].shape[0], tokenized_prompts["attention_mask"].shape[1] + 1), dtype=torch.int64, device=device)
    
        dist.broadcast(tokenized_prompts["input_ids"], src=pc.parallel_context.pp_last_rank)
        dist.broadcast(tokenized_prompts["attention_mask"], src=pc.parallel_context.pp_last_rank)
   
    # Get only the new generated tokens
    if pc.parallel_context.pp_is_last_stage:    
        for i, prompt in enumerate(prompts):
            tokenized_outputs = tokenized_prompts["input_ids"][i, tokenized_prompts["input_ids"].shape[1] - args.max_tokens:]
            outputs = tokenizer.decode(tokenized_outputs)

            print(f"Input: {prompt}")
            print(f"Output: {outputs}")
            print("------")
        