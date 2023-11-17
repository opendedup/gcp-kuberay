FROM rayproject/ray-ml:ca29fe-py310-cu118
RUN sudo DEBIAN_FRONTEND=noninteractive apt-get install -y keyboard-configuration
RUN sudo apt update && \
    sudo apt install --no-install-recommends -y cuda-11-8 && \
    sudo sudo apt-get clean
RUN pip install --upgrade pip
RUN pip install torch==2.0.1+cu118 torchvision==0.15.2+cu118 --extra-index-url https://download.pytorch.org/whl/cu118
RUN pip install -U transformers tokenizers bitsandbytes "numpy<1.24" google-cloud-storage peft pandas deepspeed torchmetrics accelerate gcsfs
#RUN pip install -U git+https://github.com/huggingface/accelerate.git
RUN pip cache purge
COPY bandbfix.py /home/ray/anaconda3/lib/python3.10/site-packages/bitsandbytes/cuda_setup/main.py
CMD ["ray", "start", "--head"]