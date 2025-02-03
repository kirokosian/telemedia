# Use an official lightweight Python runtime as a parent image.
FROM python:3.10-slim

# Set the working directory in the container.
WORKDIR /app

# Copy only necessary files
COPY requirements.txt bot.py ./

# Install dependencies
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Create directories for the SQLite database, config, movies, tv and downloads.
RUN mkdir -p /app/db /app/config /app/movies /app/tv /app/downloads

# Expose volumes so that these directories can be mounted from the host.
VOLUME ["/app/db", "/app/config", "/app/movies", "/app/tv", "/app/downloads"]

# Command to run the bot.
CMD ["python", "bot.py"]