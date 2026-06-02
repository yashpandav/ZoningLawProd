FROM python:3.11-slim

WORKDIR /app

# Install system deps (pdfplumber needs poppler, onnxruntime needs libgomp, curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download SPLADE model at build time — zero downloads at runtime
RUN python3 -c "\
from fastembed import SparseTextEmbedding; \
print('Downloading SPLADE model into image...'); \
SparseTextEmbedding(model_name='prithivida/Splade_PP_en_v1'); \
print('SPLADE model cached.')"

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
