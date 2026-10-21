FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    ca-certificates \
    libgomp1 \
    libsndfile1 \
    socat \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/coeiroink

RUN curl -L -o /tmp/coeiroink.zip "https://www.dropbox.com/scl/fi/o7xnjfo0jw81otgn2jxar/COEIROINK_LINUX_CPU_v.2025.4.1.zip?rlkey=dt1z2bxrbo3ikm2iiioxh9wx7&st=tso7516f&dl=1" && \
    unzip -q /tmp/coeiroink.zip -d /opt/coeiroink && \
    rm /tmp/coeiroink.zip && \
    chmod +x /opt/coeiroink/COEIROINK_LINUX_CPU_v.2025.4.1/engine/engine

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 50031 50032

ENTRYPOINT ["/entrypoint.sh"]
