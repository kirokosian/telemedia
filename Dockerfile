# Use an official lightweight Python runtime as a parent image.
FROM python:3.10-slim
LABEL maintainer="kirokosian <kirokosian@proton.me>"


ENV SMA_FFMPEG_URL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
ENV SMA_PATH /app/sickbeard_mp4_automator
# Set the working directory in the container.
WORKDIR /app

# Install git with apt-get
RUN apt-get update && apt-get install -y git wget tar xz-utils

# Copy only necessary files
COPY requirements.txt bot.py ./

# Remove the sickbeard dependency from installation since it'll be cloned manually.
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Clone the sickbeard_mp4_automator repository into /app/sickbeard_mp4_automator.
RUN git clone https://github.com/mdhiggins/sickbeard_mp4_automator.git ${SMA_PATH}

RUN wget ${SMA_FFMPEG_URL} -O /tmp/ffmpeg.tar.xz && \
tar xvf /tmp/ffmpeg.tar.xz -C /usr/local/bin --strip-components=1 && \
rm /tmp/ffmpeg.tar.xz && \
pip install -r ${SMA_PATH}/setup/requirements.txt
# Create directories for the SQLite database, config, movies, tv and downloads.
RUN mkdir -p /app/db /app/config /app/movies /app/tv /app/downloads

# Expose volumes so that these directories can be mounted from the host.
VOLUME ["/app/db", "/app/config", "/app/movies", "/app/tv", "/app/downloads", "/app/sickbeard_mp4_automator/config"]

# Command to run the bot.
CMD ["python", "bot.py"]