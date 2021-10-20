# -*- encoding: utf-8 -*-
'''
@File    :   inference_cogview.py
@Time    :   2021/10/09 19:41:58
@Author  :   Ming Ding
@Contact :   dm18@mail.tsinghua.edu.cn
'''

# here put the import lib
import os
import sys
import random
import time
from datetime import datetime
import torch
import torch.nn.functional as F

import mpu
from arguments import get_args
from model.glm_model import GLMModel
from training import load_checkpoint, initialize_distributed, set_random_seed, prepare_tokenizer
from tokenization import get_tokenizer
from generation.sampling_strategies import BaseStrategy
from generation.autoregressive_sampling import filling_sequence
from generation.utils import timed_name, save_multiple_images, generate_continually


def read_context(tokenizer, args, output=None):
    terminate_runs, skip_run = 0, 0
    if mpu.get_model_parallel_rank() == 0:
        while True:
            raw_text = input("\nContext prompt (stop to exit) >>> ")
            if not raw_text:
                print('Prompt should not be empty!')
                continue
            if raw_text == "stop":
                terminate_runs = 1
                break
            generation_mask = '[gMASK]' if args.task_mask else '[MASK]'
            if args.block_lm and 'MASK]' not in raw_text:
                raw_text += ' ' + generation_mask
            if output is not None:
                output.write(raw_text)
            context_tokens = tokenizer.EncodeAsIds(raw_text).tokenization
            if args.block_lm:
                context_tokens = [tokenizer.get_command('ENC').Id] + context_tokens
                if not raw_text.endswith('MASK]'):
                    context_tokens = context_tokens + [tokenizer.get_command('eos').Id]
            context_length = len(context_tokens)

            if context_length >= args.max_sequence_length:
                print("\nContext length", context_length,
                      "\nPlease give smaller context than the window length!")
                continue
            break
    else:
        context_length = 0

    terminate_runs_tensor = torch.cuda.LongTensor([terminate_runs])
    torch.distributed.broadcast(terminate_runs_tensor, mpu.get_model_parallel_src_rank(),
                                group=mpu.get_model_parallel_group())
    terminate_runs = terminate_runs_tensor[0].item()

    if terminate_runs == 1:
        return terminate_runs, None, None, None

    context_length_tensor = torch.cuda.LongTensor([context_length])

    torch.distributed.broadcast(context_length_tensor, mpu.get_model_parallel_src_rank(),
                                group=mpu.get_model_parallel_group())
    context_length = context_length_tensor[0].item()
    if mpu.get_model_parallel_rank() == 0:
        context_tokens_tensor = torch.cuda.LongTensor(context_tokens)
    else:
        context_tokens_tensor = torch.cuda.LongTensor([0] * context_length)
    torch.distributed.broadcast(context_tokens_tensor, mpu.get_model_parallel_src_rank(),
                                group=mpu.get_model_parallel_group())
    if mpu.get_model_parallel_rank() != 0:
        raw_text = tokenizer.DecodeIds(context_tokens_tensor.tolist())
    return terminate_runs, raw_text, context_tokens_tensor, context_length


def get_batch(context_tokens, device, args):
    tokens = context_tokens
    tokens = tokens.view(1, -1).contiguous()
    tokens = tokens.to(device)

    # Get the masks and postition ids.
    if args.block_lm:
        attention_mask = torch.ones(1, 1, tokens.size(1), tokens.size(1), device=device, dtype=torch.long)
        if args.fp16:
            attention_mask = attention_mask.half()
        position_ids = torch.arange(tokens.size(1), device=device, dtype=torch.long)
        if not args.no_block_position:
            block_position_ids = torch.zeros(tokens.size(1), device=device, dtype=torch.long)
            position_ids = torch.stack((position_ids, block_position_ids), dim=0)
        position_ids = position_ids.unsqueeze(0)
    else:
        raise NotImplementedError

    return tokens, attention_mask, position_ids


def top_k_logits(logits, top_k=0, top_p=0.0, filter_value=-float('Inf')):
    # This function has been mostly taken from huggingface conversational ai code at
    # https://medium.com/huggingface/how-to-build-a-state-of-the-art-conversational-ai-with-transfer-learning-2d818ac26313

    if top_k > 0:
        # Remove all tokens with a probability less than the last token of the top-k
        indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]
        logits[indices_to_remove] = filter_value

    if top_p > 0.0:
        # convert to 1D
        logits = logits.view(logits.size()[1]).contiguous()
        sorted_logits, sorted_indices = torch.sort(logits, descending=True)
        cumulative_probs = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

        # Remove tokens with cumulative probability above the threshold
        sorted_indices_to_remove = cumulative_probs > top_p
        # Shift the indices to the right to keep also the first token above the threshold
        sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0
        indices_to_remove = sorted_indices[sorted_indices_to_remove]
        logits[indices_to_remove] = filter_value
        # going back to 2D
        logits = logits.view(1, -1).contiguous()

    return logits


def sample_sequence(model, tokenizer, context_tokens, context_length, args, device, mems=None, end_tokens=None):
    if not args.block_lm:
        context_tokens, attention_mask, position_ids = get_batch(context_tokens, device, args)
        tokens = torch.empty((args.num_beams, 0), device=context_tokens.device, dtype=torch.long)
    else:
        tokens = context_tokens.new_full((1, 1), tokenizer.get_command('sop').Id)
    counter = 0
    if mems is None:
        mems = []
    if end_tokens is None:
        end_tokens = [args.eod_token]
    if args.num_beams > 1:
        beam_scorer = BeamSearchScorer(
            batch_size=1,
            max_length=args.out_seq_length,
            num_beams=args.num_beams,
            device=context_tokens.device,
            length_penalty=args.length_penalty,
            do_early_stopping=False,
        )
        beam_scores = torch.zeros(1, dtype=torch.float, device=context_tokens.device)
    last_beam_num = 1
    while counter < args.out_seq_length:
        if counter == 0 and not args.block_lm:
            next_token_logits, *mems = model(context_tokens, position_ids, attention_mask, *mems)
        else:
            if args.block_lm:
                if args.no_block_position:
                    position_ids = context_tokens.new_full((last_beam_num, 1), context_length + counter)
                else:
                    position_ids = context_tokens.new_ones(last_beam_num, 2, 1)
                    position_ids[:, 0] = context_length
                    position_ids[:, 1] = counter + 1
                attention_mask = context_tokens.new_zeros([1], device=context_tokens.device, dtype=torch.long)
            else:
                position_ids = context_tokens.new_ones((last_beam_num, 1)) * (context_length + counter - 1)
                attention_mask = context_tokens.new_ones(last_beam_num, 1, 1, args.mem_length + 1,
                                                         device=context_tokens.device, dtype=torch.float)
            last_token = tokens[:, -1:]
            next_token_logits, *mems = model(last_token, position_ids, attention_mask, *mems)
        next_token_logits = next_token_logits[:, -1]
        if args.num_beams > 1:
            next_token_scores = F.log_softmax(next_token_logits, dim=-1)
            next_token_scores = next_token_scores + beam_scores[:, None].expand_as(next_token_scores)
            vocab_size = next_token_scores.shape[-1]
            next_token_scores = next_token_scores.view(1, last_beam_num * vocab_size)

            probs = F.softmax(next_token_scores, dim=-1)
            next_tokens = torch.multinomial(probs, num_samples=2 * args.num_beams)
            next_token_scores = torch.gather(next_token_scores, -1, next_tokens)
            next_token_scores, _indices = torch.sort(next_token_scores, descending=True, dim=1)
            next_tokens = torch.gather(next_tokens, -1, _indices)

            next_indices = next_tokens // vocab_size
            next_tokens = next_tokens % vocab_size
            # stateless
            tokens = tokens.expand((args.num_beams, -1))
            beam_outputs = beam_scorer.process(
                tokens,
                next_token_scores,
                next_tokens,
                next_indices,
                eos_token_id=end_tokens,
                mems=mems
            )
            beam_scores = beam_outputs["next_beam_scores"]
            beam_next_tokens = beam_outputs["next_beam_tokens"]
            beam_idx = beam_outputs["next_beam_indices"]
            beam_next_tokens = beam_next_tokens.unsqueeze(-1)
            tokens = torch.cat([tokens[beam_idx, :], beam_next_tokens], dim=-1)
            mems = [mem[beam_idx] for mem in mems] if mems else None
            if beam_scorer.is_done:
                break
            last_beam_num = args.num_beams
        else:
            next_token_logits /= args.temperature
            next_token_logits = top_k_logits(next_token_logits, top_k=args.top_k, top_p=args.top_p)
            log_probs = F.softmax(next_token_logits, dim=-1)
            prev = torch.multinomial(log_probs, num_samples=1)[0]
            is_end = prev.item() in end_tokens
            if is_end:
                break
            prev = prev.view(1, 1)
            tokens = prev if tokens is None else torch.cat((tokens, prev), dim=1)
        counter += 1
        if not args.block_lm and mpu.get_model_parallel_rank() == 0 and counter % 16 == 0:
            output_tokens_list = tokens.view(-1).contiguous()
            decode_tokens = tokenizer.DecodeIds(output_tokens_list.tolist())
            if mpu.get_model_parallel_rank() == 0 and (counter % 128 == 0 or is_end):
                os.system('clear')
                trim_decode_tokens = decode_tokens
                print(trim_decode_tokens, flush=True)
    if args.num_beams > 1:
        tokens, mems = beam_scorer.finalize(tokens, beam_scores, next_tokens, next_indices, eos_token_id=args.eod_token,
                                            mems=mems)
    return torch.cat((context_tokens, tokens), dim=1), mems


def generate_samples(model, tokenizer, args, device):
    model.eval()
    output_path = "./samples"
    if not os.path.exists(output_path):
        os.makedirs(output_path)
    output_path = os.path.join(output_path, f"sample-{datetime.now().strftime('%m-%d-%H-%M')}.txt")
    with torch.no_grad(), open(output_path, "w") as output:
        while True:
            torch.distributed.barrier(group=mpu.get_model_parallel_group())

            terminate_runs, raw_text, context_tokens_tensor, context_length = read_context(tokenizer, args, output)
            if terminate_runs == 1:
                return
            start_time = time.time()
            if args.block_lm:
                mems = []
                tokens, attention_mask, position_ids = get_batch(context_tokens_tensor, device, args)
                mask_tokens = ['MASK', 'sMASK', 'gMASK'] if args.task_mask else ['MASK']
                mask_tokens = [tokenizer.get_command(token).Id for token in mask_tokens]
                end_tokens = [tokenizer.get_command('eop').Id, args.eod_token]
                mask_positions = []
                for token in mask_tokens:
                    mask_positions += (context_tokens_tensor == token).nonzero(as_tuple=True)[0].tolist()
                mask_positions.sort()
                if args.no_block_position:
                    for mask_position in mask_positions:
                        position_ids[0, mask_position + 1:] += args.out_seq_length
                _, *mems = model(tokens, position_ids, attention_mask, *mems)
                for mask_position in mask_positions:
                    if args.no_block_position:
                        position = position_ids[0, mask_position].item()
                    else:
                        position = mask_position
                    tokens, mems = sample_sequence(model, tokenizer, tokens, position,
                                                   args, device, mems=mems, end_tokens=end_tokens)
            else:
                tokens, _ = sample_sequence(model, tokenizer, context_tokens_tensor, context_length, args, device)
            output_tokens_list = tokens.view(-1).contiguous()
            if mpu.get_model_parallel_rank() == 0:
                os.system('clear')
                print("\nTaken time {:.2f}\n".format(time.time() - start_time), flush=True)
                print("\nContext:", raw_text, flush=True)
                decode_tokens = tokenizer.DecodeIds(output_tokens_list.tolist())
                trim_decode_tokens = decode_tokens
                print("\nGLM:", trim_decode_tokens, flush=True)
                output.write(trim_decode_tokens + "\n")

            torch.distributed.barrier(group=mpu.get_model_parallel_group())


def main(args):
    initialize_distributed(args)
    tokenizer = prepare_tokenizer(args)
    args.eod_token = tokenizer.get_command('eos').Id
    # build model
    model = GLMModel(args)
    if args.fp16:
        model = model.half()
    model = model.to(args.device)
    load_checkpoint(model, args)
    set_random_seed(args.seed)
    model.eval()
    generate_samples(model, tokenizer, args, torch.cuda.current_device())


if __name__ == "__main__":
    args = get_args()

    with torch.no_grad():
        main(args)
