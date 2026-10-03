# One image for every target: AWS Lambda (via Lambda Web Adapter), any Docker host, Hugging Face Spaces.
FROM python:3.11-slim

# AWS Lambda Web Adapter: on Lambda it turns invocations into HTTP requests to the app on port 7860.
# Anywhere else this extension file is simply ignored.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

# Build everything as user 1000 (Hugging Face requirement). Lambda runs as its own user with
# HOME=/tmp, so the files are made world-readable at the end and PYTHONUSERBASE pins where the
# pip --user packages live (otherwise Python would look under $HOME and not find them).
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH \
    PYTHONUSERBASE=/home/user/.local PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/home/user/app/src HF_HOME=/home/user/.cache/hf AWS_LWA_PORT=7860
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
# CPU-only PyTorch (much smaller than the default CUDA build)
RUN pip install --user --index-url https://download.pytorch.org/whl/cpu torch && \
    pip install --user -r requirements.txt

# Pre-download the retrieval models so the first request is fast (and works offline on Lambda)
RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('BAAI/bge-small-en-v1.5'); CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

COPY --chown=user src ./src
# The index is built on your machine (rebuild.bat) and shipped with the image
COPY --chown=user data ./data
# (as root: WORKDIR created /home/user/app as root, so user 1000 may not chmod it)
USER root
RUN chmod -R a+rX /home/user
USER user

EXPOSE 7860
CMD ["python", "-m", "uvicorn", "asof.web.server:app", "--host", "0.0.0.0", "--port", "7860"]
