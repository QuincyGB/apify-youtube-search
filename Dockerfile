FROM apify/actor-python:3.12

# Copy just requirements and install them first, to cache the layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the actor code.
COPY . ./

CMD ["python", "-m", "src.main"]
