FROM ubuntu:20.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    curl \
    tar \
    ca-certificates \
    libgomp1 \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/sharevox

RUN curl -sL "https://github.com/SHAREVOX/sharevox/releases/download/0.2.1/sharevox-linux-cpu-0.2.1.tar.gz" | tar -xzf - --strip-components=1 && \
    chmod +x /opt/sharevox/run

EXPOSE 50025

ENTRYPOINT ["/opt/sharevox/run"]
CMD ["--host", "0.0.0.0", "--port", "50025"]
