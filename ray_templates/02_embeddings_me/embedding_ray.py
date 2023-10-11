import binascii
import io
from typing import List,Dict

import pypdf
import ray
from pypdf import PdfReader
import json
import uuid
import traceback


ray.init(
    runtime_env={"pip": ["langchain", "pypdf", "sentence_transformers", "transformers"]}
)

from ray.data.datasource import FileExtensionFilter

# Filter out non-PDF files.
# The GCS bucket is public and contains all the PDF documents, as well as a CSV file containing the licenses for each.
ds = ray.data.read_binary_files("gs://ray-llm-batch-inference-uscentral/100files/", partition_filter=FileExtensionFilter("txt"))
print(ds)
# We use pypdf directly to read PDF directly from bytes.
# LangChain can be used instead once https://github.com/hwchase17/langchain/pull/3915
# is merged.
def convert_to_text(file_row: Dict[str, bytes]) -> List[Dict[str, str]]:
    try:
        text = file_row["bytes"].decode(encoding='utf-8')
        return [{"page": text}]
    except:
        return []
    

# We use `flat_map` as `convert_to_text` has a 1->N relationship.
# It produces N strings for each PDF (one string per page).
# Use `map` for 1->1 relationship.
print(ds)
ds = ds.flat_map(convert_to_text)

from langchain.text_splitter import RecursiveCharacterTextSplitter

def split_text(page_text: Dict[str, str]):
    # Use chunk_size of 1000.
    # We felt that the answer we would be looking for would be 
    # around 200 words, or around 1000 characters.
    # This parameter can be modified based on your documents and use case.
    split_text = []
    ll = len(page_text["page"])
    if ll > 101:
        try:
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=1000, chunk_overlap=100,length_function = len
            )
            
            split_text: List[str] = text_splitter.split_text(page_text["page"])
            split_text = [text.replace("\n", " ") for text in split_text]
        except:
            print(f""""unable to split text {ll}""")
            print(traceback.format_exc())
          
        return [{"text": text}  for text in split_text]
    else:
        return[{"text": page_text["page"]}]




# We use `flat_map` as `split_text` has a 1->N relationship.
# It produces N output chunks for each input string.
# Use `map` for 1->1 relationship.
ds = ds.flat_map(split_text)

from sentence_transformers import SentenceTransformer

# Use LangChain's default model.
# This model can be changed depending on your task.
model_name = "sentence-transformers/all-mpnet-base-v2"


# We use sentence_transformers directly to provide a specific batch size.
# LangChain's HuggingfaceEmbeddings can be used instead once https://github.com/hwchase17/langchain/pull/3914
# is merged.
class Embed:
    def __init__(self):
        # Specify "cuda" to move the model to GPU.
        try:
            self.transformer = SentenceTransformer(model_name, device="cuda")
        except:
            print("unable to do embedding")
            print(traceback.format_exc())

    def __call__(self, text_batch: Dict[str, str]):
        # We manually encode using sentence_transformer since LangChain
        # HuggingfaceEmbeddings does not support specifying a batch size yet.
        try:
            text_batch["embedding"] = self.transformer.encode(
                text_batch["text"],
                batch_size=100,  # Large batch size to maximize GPU utilization.
                device="cuda",
            ).tolist()
        except:
            print("unable to do embedding")
            print(traceback.format_exc())

        return text_batch


# Use `map_batches` since we want to specify a batch size to maximize GPU utilization.
ds = ds.map_batches(
    Embed,
    # Large batch size to maximize GPU utilization.
    # Too large a batch size may result in GPU running out of memory.
    # If the chunk size is increased, then decrease batch size.
    # If the chunk size is decreased, then increase batch size.
    batch_size=100,  # Large batch size to maximize GPU utilization.
    compute=ray.data.ActorPoolStrategy(min_size=16, max_size=16),  # I have 20 GPUs in my cluster
    num_gpus=1,  # 1 GPU for each actor.
)

import jsonlines
with jsonlines.open('embeddings-100.json', mode='w') as writer:
    for output in ds.iter_rows():
        writer.write(output)

print("done")

