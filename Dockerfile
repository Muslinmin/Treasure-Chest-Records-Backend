# 1. Base image
FROM python:3.12-slim

# 2. Set the working directory inside the container
WORKDIR /app

# 3. Leverage layer caching: Copy only requirements first
COPY requirements.txt .

# 4. Install dependencies (cached unless requirements.txt changes)
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy the rest of the application code
COPY . .

# 6. Expose the port Uvicorn will run on
EXPOSE 8000

# 7. Command to start up the server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]