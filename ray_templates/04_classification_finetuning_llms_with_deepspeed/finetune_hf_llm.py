import argparse
from filelock import FileLock
import functools
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import tree
from typing import Tuple
import pandas as pd
from datasets import Dataset

try:
    import deepspeed  # noqa: F401
except ImportError as e:
    raise RuntimeError(
        "Please install deepspeed with `pip install --user deepspeed`."
    ) from e

from accelerate import Accelerator, DeepSpeedPlugin
from accelerate.utils import DummyOptim, DummyScheduler, set_seed
import torch
import torch.nn as nn
import tqdm
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    get_linear_schedule_with_warmup,
)

import ray
from ray import train
import ray.util.scheduling_strategies
from ray.train.torch import TorchTrainer
from ray.train import Checkpoint



import urllib.parse
import os
from google.cloud import storage
import hashlib


OPTIM_BETAS = (0.9, 0.999)
OPTIM_EPS = 1e-8
OPTIM_WEIGHT_DECAY = 0.0

def download_to_local(url,local_folder):
    print(f'Downloading {url} to {local_folder}')
    # Get the bucket name
    bucket_name = urllib.parse.urlparse(url).netloc

    # Get the path
    folder = urllib.parse.urlparse(url).path[1:]
    print(folder)
    storage_client = storage.Client()

    bucket = storage_client.bucket(bucket_name)

    blobs=bucket.list_blobs(prefix=folder, delimiter="/") #List all objects that satisfy the filter.
    hash_value = hashlib.sha256(folder.encode()).hexdigest()
    local_folder = '{}/model_{}'.format(local_folder, hash_value) 
    
    #  Create this folder locally if not exists
    if not os.path.exists(local_folder):
        os.makedirs(local_folder)

    # Iterating through for loop one by one using API call
    for blob in blobs:
        if not blob.name.endswith("/"):
            fn = os.path.basename(blob.name)
            print(fn)
            destination_uri = '{}/{}'.format(local_folder, fn)
            if not os.path.exists(destination_uri):
                blob.download_to_filename(destination_uri)
                print('Downloaded {} to {}'.format(blob.name, destination_uri))
    return local_folder

def get_number_of_params(model: nn.Module):
    state_dict = model.state_dict()
    return sum(p.numel() for p in state_dict.values())


def collate_fn(batch, tokenizer, block_size, device):

    out_batch = tokenizer(
        list(batch["text"]),
        padding=True,
        max_length=block_size,
        truncation=True,
        return_tensors="pt",
    )
    out_batch["labels"] = torch.LongTensor([sample for sample in  list(batch["label"])])

    out_batch = tree.map_structure(lambda x: x.to(device), out_batch)

    return out_batch


def get_tokenizer(model_name):

    # Context for legacy=True: https://github.com/huggingface/transformers/issues/25176
    tokenizer = AutoTokenizer.from_pretrained(model_name, legacy=True)
    tokenizer.pad_token = tokenizer.eos_token
    #tokenizer.add_tokens(special_tokens, special_tokens=True)

    return tokenizer


def evaluate(
    *, model, eval_ds, accelerator, bsize, ds_kwargs, as_test: bool = False
) -> Tuple[float, float]:
    model.eval()
    losses = []

    eval_dataloader = eval_ds.iter_torch_batches(batch_size=bsize, **ds_kwargs)
    eval_ds_len = len(list(eval_ds.iter_batches(batch_size=1)))
    for step, batch in tqdm.tqdm(
        enumerate(eval_dataloader), total=eval_ds_len // (bsize + 1)
    ):
        with torch.no_grad():
            outputs = model(**batch)

        loss = outputs.loss
        # The tensors are gathered by concatenating them on the first dimension, so we
        # add a new dimension to the scalar loss to get a tensor of shape (K,) for K
        # workers.
        losses.append(accelerator.gather(loss[None]))

        if as_test:
            break

    # We stack losses so that we have a tensor of shape (T, K) where T is the number of
    # steps and K is the number of workers.
    losses = torch.stack(losses)
    try:
        eval_loss = torch.mean(losses).item()
        perplexity = math.exp(eval_loss)
    except OverflowError:
        perplexity = float("inf")
    return perplexity, eval_loss


def checkpoint_model(
    checkpoint_folder, ckpt_id, model, epoch, last_global_step, **kwargs
):
    """Utility function for checkpointing model + optimizer dictionaries
    The main purpose for this is to be able to resume training from that instant again.
    """
    checkpoint_state_dict = {
        "epoch": epoch,
        "last_global_step": last_global_step,
    }
    # Add extra kwargs too
    checkpoint_state_dict.update(kwargs)

    # In here model will be a DeepspeedEngine object
    model.save_checkpoint(checkpoint_folder, ckpt_id, checkpoint_state_dict)
    status_msg = (
        f"checkpointing: checkpoint_folder={checkpoint_folder}, ckpt_id={ckpt_id}"
    )
    print(status_msg)


def training_function(kwargs: dict):
    print("training_function called")
    
    # Train has a bug somewhere that causes ACCELERATE_TORCH_DEVICE to not be set
    # properly on multi-gpu nodes
    cuda_visible_device = os.environ["CUDA_VISIBLE_DEVICES"].split(",")
    local_rank = int(os.environ["LOCAL_RANK"])
    device_id = cuda_visible_device[local_rank]
    os.environ["ACCELERATE_TORCH_DEVICE"] = f"cuda:{device_id}"

    config = kwargs["config"]
    args = argparse.Namespace(**kwargs["args"])
    model_id = config["model_name"]
    model_revision = config["model_revision"]
    if model_id.startswith("gs://"):
        model_id = download_to_local(model_id,"/tmp")
    # Sample hyper-parameters for learning rate, batch size, seed and a few other HPs
    lr = config["lr"]
    num_epochs = int(config["num_epochs"])
    seed = int(config["seed"])
    batch_size = int(config["batch_size"])
    gradient_accumulation_steps = int(config["gradient_accumulation_steps"])

    # Get deepspeed config to setup the batch size per device
    ds_plugin = config["ds_plugin"]
    ds_plugin.hf_ds_config.config["train_micro_batch_size_per_gpu"] = batch_size

    # Initialize accelerator
    accelerator = Accelerator(
        deepspeed_plugin=ds_plugin,
        gradient_accumulation_steps=gradient_accumulation_steps,
        mixed_precision=args.mx,
    )

    set_seed(seed)

    # train_ds is the local shard for this model
    train_ds = train.get_dataset_shard("train")
    valid_ds = train.get_dataset_shard("valid")

    train_ds_len = len(list(train_ds.iter_batches(batch_size=1)))

    tokenizer = get_tokenizer(model_name=model_id)
    collate_partial = functools.partial(
        collate_fn,
        tokenizer=tokenizer,
        block_size=config["block_size"],
        device=accelerator.device,
    )

    s = time.time()
    model = AutoModelForSequenceClassification.from_pretrained(
        model_id,
        num_labels=2,
        trust_remote_code=True,
        torch_dtype=torch.bfloat16,
        # `use_cache=True` is incompatible with gradient checkpointing.
        use_cache=False,
        max_position_embeddings=config["block_size"],
        revision=model_revision
    )
    model.config.pad_token_id = model.config.eos_token_id
    print(f"Done loading model in {time.time() - s} seconds.")
    
    print("Model initialized with pretrained weights. Training starting...")
    # peft_config = LoraConfig(
    #      task_type=TaskType.SEQ_CLS, inference_mode=False, r=8, lora_alpha=32, lora_dropout=0.1,
    #      bias="none",
    # )
    # model = get_peft_model(model, peft_config)
    model.resize_token_embeddings(len(tokenizer))
    #model.print_trainable_parameters()
    if not args.no_grad_ckpt:
        model.gradient_checkpointing_enable()

    optimizer_cls = (
        torch.optim.AdamW
        if accelerator.state.deepspeed_plugin is None
        or "optimizer" not in accelerator.state.deepspeed_plugin.deepspeed_config
        else DummyOptim
    )


    optimizer = optimizer_cls(
        model.parameters(),
        lr=lr,
        betas=OPTIM_BETAS,
        weight_decay=OPTIM_WEIGHT_DECAY,
        eps=OPTIM_EPS,
    )

    # Instantiate scheduler
    # Creates Dummy Scheduler if `scheduler` was specified in the config file
    # else, creates `args.lr_scheduler_type` Scheduler
    # get train and valid dataset lengths

    if (
        accelerator.state.deepspeed_plugin is None
        or "scheduler" not in accelerator.state.deepspeed_plugin.deepspeed_config
    ):
        lr_scheduler = get_linear_schedule_with_warmup(
            optimizer=optimizer,
            num_warmup_steps=100,
            num_training_steps=(
                (train_ds_len * num_epochs) // gradient_accumulation_steps
            ),
        )
    else:
        lr_scheduler = DummyScheduler(
            optimizer,
            total_num_steps=(train_ds_len * num_epochs) // gradient_accumulation_steps,
            warmup_num_steps=100,
        )

    # Prepare everything
    # There is no specific order to remember, we just need to unpack the objects in the
    # same order we gave them to the prepare method.
    s = time.time()
    model, optimizer, lr_scheduler = accelerator.prepare(model, optimizer, lr_scheduler)
    print(f"Prepare done in {time.time() - s} seconds.")

    # Now we train the model
    if accelerator.is_main_process:
        print("Starting training ...")
        print("Number of batches on main process", train_ds_len // batch_size)

    for epoch in range(num_epochs):

        fwd_time_sum, bwd_time_sum, optim_step_time_sum = 0, 0, 0
        s_epoch = time.time()
        model.train()
        loss_sum = torch.tensor(0.0).to(accelerator.device)

        train_dataloader = train_ds.iter_torch_batches(
            batch_size=batch_size,
            collate_fn=collate_partial,
        )

        for step, batch in tqdm.tqdm(
            enumerate(train_dataloader), total=train_ds_len // batch_size + 1
        ):

            # We could avoid this line since we set the accelerator with
            # `device_placement=True`.
            with accelerator.accumulate(model):
                s_fwd = time.time()
                outputs = model(**batch)
                loss = outputs.loss
                loss_sum += loss
                e_fwd = time.time()
                fwd_time = e_fwd - s_fwd
                fwd_time_sum += fwd_time
                s_bwd = time.time()
                accelerator.backward(loss)
                e_bwd = time.time()
                bwd_time = e_bwd - s_bwd
                bwd_time_sum += bwd_time

                s_opt_step = time.time()
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()
                e_opt_step = time.time()
                optim_step_time_sum += e_opt_step - s_opt_step

            if accelerator.is_main_process:
                accelerator.print(
                    f"[epoch {epoch} step {step}] "
                    f"loss: {loss.item()} step-time: {e_opt_step - s_fwd}"
                )

            if config["as_test"]:
                break

            # as long as this is not the last step report here
            if step != (train_ds_len // batch_size - 1):
                aggregated_loss = torch.mean(accelerator.gather(loss[None])).item()
                train.report(
                    {
                        "epoch": epoch,
                        "iteration": step,
                        "train_loss_batch": aggregated_loss,
                        "avg_train_loss_epoch": None,
                        "eval_loss": None,
                        "perplexity": None,
                        "num_iterations": step + 1,
                        "train_time_per_epoch": None,
                        "eval_time_per_epoch": None,
                        "fwd_time": fwd_time,
                        "bwd_time": bwd_time,
                        "avg_fwd_time_per_epoch": None,
                        "avg_bwd_time_per_epoch": None,
                        "learning_rate": lr_scheduler.get_lr()[0],
                    }
                )

        e_epoch = time.time()
        accelerator.print("Train time per epoch: ", e_epoch - s_epoch)

        eval_s_epoch = time.time()
        print("Running evaluation ...")
        perplex, eloss = evaluate(
            model=model,
            eval_ds=valid_ds,
            accelerator=accelerator,
            bsize=config["eval_batch_size"],
            ds_kwargs={"collate_fn": collate_partial},
            as_test=config["as_test"],
        )
        accelerator.print("Eval result loss", eloss)
        accelerator.print("Eval perplex", perplex)

        eval_e_epoch = time.time()
        accelerator.print("Eval time per epoch: ", eval_e_epoch - eval_s_epoch)
        accelerator.print("avg fwd time: ", fwd_time_sum / (step + 1))
        accelerator.print("avg bwd time: ", bwd_time_sum / (step + 1))
        accelerator.print("avg opt step time: ", optim_step_time_sum / (step + 1))

        metrics = {
            "epoch": epoch,
            "iteration": step,
            "train_loss_batch": loss.item(),
            "avg_train_loss_epoch": loss_sum.item() / (step + 1),
            "eval_loss": eloss,
            "perplexity": perplex,
            "num_iterations": step + 1,
            "train_time_per_epoch": e_epoch - s_epoch,
            "eval_time_per_epoch": eval_e_epoch - eval_s_epoch,
            "fwd_time": fwd_time,
            "bwd_time": bwd_time,
            "avg_fwd_time_per_epoch": fwd_time_sum / (step + 1),
            "avg_bwd_time_per_epoch": bwd_time_sum / (step + 1),
            "learning_rate": lr_scheduler.get_lr()[0],
        }

        with tempfile.TemporaryDirectory() as temp_checkpoint_dir:
            accelerator.print(f"Saving the model locally at {temp_checkpoint_dir}")
            accelerator.wait_for_everyone()

            checkpoint_save_start = time.perf_counter()

            if accelerator.is_main_process:
                print("Saving tokenizer and config.")
                tokenizer.save_pretrained(temp_checkpoint_dir)

            accelerator.wait_for_everyone()

            # Checkpointing strategy 1: Distributed checkpointing
            # This checkpointing method makes deepspeed checkpoints on each node
            # and then Ray Train will aggregate them to a central s3 bucket.
            # It should be done on all processes (not just the Rank 0)
            # aggregate_on_rank_0 = False
            # checkpoint_model(
            #      checkpoint_folder=temp_checkpoint_dir,
            #      ckpt_id=epoch,
            #      model=model,
            #      epoch=epoch,
            #      last_global_step=step
            # )

            # Checkpointing strategy 2: Aggregate model on the rank 0 worker then upload
            aggregate_on_rank_0 = True
            unwrapped_model = accelerator.unwrap_model(model)
            unwrapped_model.save_pretrained(
                temp_checkpoint_dir,
                is_main_process=accelerator.is_main_process,
                save_function=accelerator.save,
                safe_serialization=True,
                state_dict=accelerator.get_state_dict(model),
            )
            accelerator.wait_for_everyone()
            print("Checkpoint save time: ", time.perf_counter() - checkpoint_save_start)

            checkpoint_upload_start = time.perf_counter()

            # Create the checkpoint object to report to Ray Train and upload to storage.
            # If we aggregated the model on rank 0, we only need to report
            # the checkpoint from the rank 0 worker, since all other checkpoint
            # directories are empty (`save_pretrained` was a noop for other workers).
            if aggregate_on_rank_0:
                checkpoint = (
                    Checkpoint.from_directory(temp_checkpoint_dir)
                    if accelerator.is_main_process
                    else None
                )
            else:
                # Distributed checkpointing should upload shards from each worker.
                checkpoint = Checkpoint.from_directory(temp_checkpoint_dir)

            # Note: After `train.report`, in the case of remote storage,
            # the checkpoint directory will be uploaded to the remote storage.
            train.report(metrics, checkpoint=checkpoint)

            print(
                "Checkpoint upload time: ",
                time.perf_counter() - checkpoint_upload_start,
            )
            print(
                "Total checkpointing time: ",
                time.perf_counter() - checkpoint_save_start,
            )


def parse_args():

    parser = argparse.ArgumentParser(description="Simple example of training script.")
    parser.add_argument(
        "--mx",
        type=str,
        default="bf16",
        choices=["no", "fp16", "bf16", "fp8"],
        help="Whether to use mixed precision. Choose"
        "between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >= 1.10."
        "and an Nvidia Ampere GPU.",
    )

    parser.add_argument(
        "--batch-size-per-device",
        "-bs",
        type=int,
        default=16,
        help="Batch size to use per device.",
    )

    parser.add_argument(
        "--eval-batch-size-per-device",
        type=int,
        default=64,
        help="Batch size to use per device (For evaluation).",
    )

    parser.add_argument(
        "--num-devices", "-nd", type=int, default=4, help="Number of devices to use."
    )
    parser.add_argument(
        "--grad_accum", type=int, default=1, help="Gradient accumulation steps."
    )
    parser.add_argument(
        "--no-grad-ckpt",
        action="store_true",
        help="If passed, will not use gradient checkpointing.",
    )
    parser.add_argument("--output_dir", type=str, help="Path to output directory.")
    parser.add_argument(
        "--model_name", default="meta-llama/Llama-2-7b-hf", type=str
    )
    parser.add_argument(
        "--model_revision", default="main", type=str
    )
    parser.add_argument(
        "--num-epochs", type=int, default=1, help="Number of epochs to train for."
    )
    parser.add_argument(
        "--num-checkpoints-to-keep",
        type=int,
        help=(
            "Number of checkpoints to keep, if None, all checkpoints will be kept, "
            "if set to n>=1, the top n checkpoint with min. evaluation perplexity "
            "will be kept."
        ),
        default=None,
    )
    parser.add_argument("--lr", type=float, default=5e-6, help="Learning rate to use.")

    parser.add_argument(
        "--ctx-len", type=int, default=512, help="Learning rate to use."
    )
    
    parser.add_argument(
        "--dataset"
        , default=None, type=str
    )

    parser.add_argument(
        "--as-test",
        action="store_true",
        help="If passed, will run the script in test mode.",
    )

    parser.add_argument(
        "--ds-config",
        type=str,
        default="./deepspeed_configs/zero_3_llama_2_7b.json",
        help="Deepspeed config json to use.",
    )
    args = parser.parse_args()

    return args


def main():

    args = parse_args()

    if not args.output_dir:
        raise ValueError("--output_dir must be specified")
    if not args.dataset:
        raise ValueError("--dataset must be specified")

    # update the config with args so that we have access to them.
    config = vars(args)
    config.update(
        **{
            "lr": args.lr,
            "num_epochs": args.num_epochs,
            "seed": 42,
            "batch_size": args.batch_size_per_device,
            "gradient_accumulation_steps": args.grad_accum,
            "model_name": args.model_name,
            "block_size": args.ctx_len,
            "eval_batch_size": args.eval_batch_size_per_device,
        }
    )

    # Add deepspeed plugin to the config
    ds_plugin = DeepSpeedPlugin(hf_ds_config=config.get("ds_config"))
    config.update(ds_plugin=ds_plugin)

    os.environ["RAY_AIR_LOCAL_CACHE_DIR"] = args.output_dir

    ray.init(
        runtime_env={
            "env_vars": {
                "RAY_AIR_LOCAL_CACHE_DIR": os.environ["RAY_AIR_LOCAL_CACHE_DIR"],
                "HUGGING_FACE_HUB_TOKEN": os.environ["HUGGING_FACE_HUB_TOKEN"],
                "RAY_memory_monitor_refresh_ms": "0",
            },
            "pip": [
                "transformers>=4.34.0",
                "gcsfs",
                "deepspeed[1bit_adam]"
            ],
            "excludes":["/data"],
            "working_dir": ".",
        }
    )

    # Read data
    df = pd.read_json(args.dataset, lines=True)
    nflclass = Dataset.from_pandas(df)
    nflclass = nflclass.shuffle(seed=config["seed"])
    nflclass = nflclass.train_test_split(test_size=0.01)
    trainset = nflclass["train"]
    print(nflclass)
    train_ds = ray.data.from_huggingface(trainset)
    valid_ds = ray.data.from_huggingface(nflclass["test"])
   


    assert (
        "ANYSCALE_ARTIFACT_STORAGE" in os.environ
    ), "ANYSCALE_ARTIFACT_STORAGE env var must be set!"
    artifact_storage = os.environ["ANYSCALE_ARTIFACT_STORAGE"]
    user_name = re.sub(r"\s+", "__", os.environ.get("ANYSCALE_USERNAME", "user"))
    storage_path = (
        f"{artifact_storage}/{user_name}/ft_llms_with_deepspeed/"
    )

    trainer = TorchTrainer(
        training_function,
        train_loop_config={
            "config": config,
            "args": vars(args),
        },
        run_config=train.RunConfig(
            storage_path=storage_path,
            checkpoint_config=train.CheckpointConfig(
                num_to_keep=args.num_checkpoints_to_keep,
                checkpoint_score_attribute="perplexity",
                checkpoint_score_order="min",
            ),
        ),
        scaling_config=train.ScalingConfig(
            num_workers=args.num_devices,
            use_gpu=True,
            resources_per_worker={"GPU": 1, "CPU":5},
        ),
        datasets={"train": train_ds, "valid": valid_ds},
        dataset_config=ray.train.DataConfig(datasets_to_split=["train", "valid"]),
    )

    result: train.Result = trainer.fit()
    # `best_checkpoints` are sorted in increasing score order.
    # (Ex: in this case, negative perplexity, since we set `checkpoint_score_order=min`)
    best_checkpoint, best_checkpoint_metrics = result.best_checkpoints[-1]

    print("Results are stored at:")
    print(result.path)
    print("Best checkpoint is stored at:")
    print(best_checkpoint)
    print(f"With perplexity: {best_checkpoint_metrics['perplexity']}")


if __name__ == "__main__":
    main()