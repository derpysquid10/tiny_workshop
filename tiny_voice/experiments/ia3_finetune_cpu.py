from datasets import load_from_disk
from transformers import WhisperProcessor, WhisperTokenizer, WhisperForConditionalGeneration
from transformers import Seq2SeqTrainingArguments, Seq2SeqTrainer
import torch
from dataclasses import dataclass
from typing import Any, Dict, List, Union
import evaluate
import wandb
import time
import sys
import os
from peft import IA3Config, IA3Model
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from config import HF_CACHE_DIR, PROCESSED_DATA_DIR, MODEL_NAME, MODELS_DIR
from datetime import datetime

# Global variables
today_date = datetime.now().date()
DATASET = "isizulu"
EXPERIMENT_NAME = f"ia3_finetune_gpu_{today_date}"
EXPERIMENT_TAG = ["no_decay", "gpu", "ia3", DATASET, MODEL_NAME, f"{today_date}"]

@dataclass
class DataCollatorSpeechSeq2SeqWithPadding:
    processor: Any
    decoder_start_token_id: int

    def __call__(self, features: List[Dict[str, Union[List[int], torch.Tensor]]]) -> Dict[str, torch.Tensor]:
        """
        The data collator is a function that is responsible for taking a list of features and converting them into a torch tensor.
        This is where we do the padding for the model inputs and the labels.
        This is a simplified version of the data collator provided by hugging face.

        Args:
            features: List of features. Each feature is a dictionary containing the input features and the labels.
        
        Returns:
            Dict[str, torch.Tensor]: A dictionary containing the model inputs and the labels as correctly padded torch tensors.
        """

        # first treat the audio inputs by simply returning torch tensors
        input_features = [{"input_features": feature["input_features"]} for feature in features]
        batch = self.processor.feature_extractor.pad(input_features, return_tensors="pt")

        # get the tokenized label sequences and pad labels to max length
        label_features = [{"input_ids": feature["labels"]} for feature in features]
        labels_batch = self.processor.tokenizer.pad(label_features, return_tensors="pt")

        # replace padding with -100 to ignore loss correctly
        labels = labels_batch["input_ids"].masked_fill(labels_batch.attention_mask.ne(1), -100)

        # if bos (beginning of sentence) token is appended in previous tokenization step,
        # cut bos token here as it's append later anyways
        if (labels[:, 0] == self.decoder_start_token_id).all().cpu().item():
            labels = labels[:, 1:]

        batch["labels"] = labels
        return batch


class CustomTrainer(Seq2SeqTrainer):
    def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
        # Remove any extra keys that the model doesn't support
        return super().compute_loss(model, inputs, return_outputs=return_outputs)


def make_inputs_require_grad(module, input, output):
    output.requires_grad_(True)


def compute_metrics(pred: any) -> Dict[str, float]:
    """
    Compute the evaluation metric for the predicted transcript
    """
    pred_ids = pred.predictions
    label_ids = pred.label_ids

    # replace -100 with the pad_token_id
    tokenizer = WhisperTokenizer.from_pretrained(MODEL_NAME, cache_dir=HF_CACHE_DIR)
    label_ids[label_ids == -100] = tokenizer.pad_token_id

    # we do not want to group tokens when computing the metrics
    pred_str = tokenizer.batch_decode(pred_ids, skip_special_tokens=True)
    label_str = tokenizer.batch_decode(label_ids, skip_special_tokens=True)

    metric=evaluate.load("wer")
    wer = 100 * metric.compute(predictions=pred_str, references=label_str)

    return {"wer": wer}


def train_cpu():
    print("Loading data...")
    afrispeech = load_from_disk(f"{PROCESSED_DATA_DIR}_{DATASET}")
    afrispeech_split = load_from_disk(f"{PROCESSED_DATA_DIR}_split_{DATASET}")
    processor = WhisperProcessor.from_pretrained(MODEL_NAME, cache_dir=HF_CACHE_DIR, language="English", task="transcribe")

    print("Loading pre-trained model...")
    model = WhisperForConditionalGeneration.from_pretrained(MODEL_NAME)
    model.generation_config.language = "English"
    model.generation_config.task = "transcribe"

    # IA3 configuration
    # for name, param in model.named_parameters():
    #     print(name, param.requires_grad)
    config = IA3Config(peft_type="IA3", target_modules=["k_proj", "v_proj", "q_proj", "fc1", "fc2"], feedforward_modules=["fc1", "fc2"])
    model = IA3Model(config=config, model=model, adapter_name="ia3")
    
    # Count and print trainable parameters
    trainable_params = 0
    all_params = 0
    for name, param in model.named_parameters():
        all_params += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    
    print(f"Trainable parameters: {trainable_params}")
    print(f"All parameters: {all_params}")
    print(f"Trainable%: {100 * trainable_params / all_params:.2f}%")
    # model.print_trainable_parameters()

    print("Setting data collator, eval metrics, and training arguments...")
    data_collator = DataCollatorSpeechSeq2SeqWithPadding(
        processor=processor,
        decoder_start_token_id=model.config.decoder_start_token_id,
    )

    # Register the hook to make the initial inputs require grad
    model.model.get_encoder().conv1.register_forward_hook(make_inputs_require_grad)
    
    # Define the evaluation metric (word error rate)
    wer_metric = evaluate.load("wer")

    wandb.init(
        project="tiny_voice",
        name=f"{EXPERIMENT_NAME}",
        tags=EXPERIMENT_TAG,
    )

    # Define the training arguments
    batch_size = 8
    max_steps = 200
    training_args = Seq2SeqTrainingArguments(
        output_dir= MODELS_DIR / f"{EXPERIMENT_NAME}", 
        per_device_train_batch_size=batch_size,
        gradient_accumulation_steps=1, 
        learning_rate=2e-3,
        lr_scheduler_type="constant",
        warmup_steps=20,
        # num_train_epochs=1,
        max_steps=max_steps,
        gradient_checkpointing=True,
        fp16=True,
        eval_strategy="steps",
        per_device_eval_batch_size=8,
        predict_with_generate=True,
        generation_max_length=100,
        save_steps=25,
        eval_steps=25,
        logging_steps=5,
        report_to=["wandb"],
        load_best_model_at_end=True,
        metric_for_best_model="wer",
        greater_is_better=False,
        push_to_hub=False,
        use_cpu=False,
        use_ipex=False,
        remove_unused_columns=False,
        label_names=["labels"],
        save_safetensors=False,
    )

    # Create the trainer
    trainer = CustomTrainer(
        args=training_args,
        model=model,
        train_dataset=afrispeech["train"],
        eval_dataset=afrispeech["test"],
        data_collator=data_collator,
        compute_metrics=compute_metrics,
        tokenizer=processor.feature_extractor,
    )

    # Evaluate the pretrained model before fine tuning
    print("Evaluating the pre-trained model...")
    eval_results = trainer.evaluate()
    print("Evaluation results: ", eval_results)

    # Train the model
    print("Training the model...")
    start_time = time.time()
    trainer.train()
    end_time = time.time()
    # time_per_sample = (end_time - start_time) / (max_steps * batch_size)
    # wandb.log({"time_per_sample": time_per_sample})

    # Final evaluation
    print("Evaluating the finetuned model...")
    eval_results = trainer.evaluate()
    print("Evaluation results: ", eval_results)
    wandb.log({
        "final_overall_wer": eval_results["eval_wer"],
    })

    print("Evaluating on general domain...")
    eval_results_general = trainer.evaluate(eval_dataset=afrispeech_split["test_general"])
    print("General domain fine-tuned model WER: ", eval_results_general)
    wandb.log({
        "general_domain_wer": eval_results_general["eval_wer"],
    })

    # Evaluate on "clinical" as well
    print("Evaluating on clinical domain...")
    eval_results_clinical = trainer.evaluate(eval_dataset=afrispeech_split["test_clinical"])
    print("Clinical domain fine-tuned model WER: ", eval_results_clinical)
    wandb.log({
        "clinical_domain_wer": eval_results_clinical["eval_wer"],
    })

if __name__ == "__main__":
    train_cpu()