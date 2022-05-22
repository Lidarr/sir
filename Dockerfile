ARG PYTHON_VERSION=2.7
ARG BASE_IMAGE_DATE=20220421
FROM metabrainz/python:$PYTHON_VERSION-$BASE_IMAGE_DATE

ARG SIR_VERSION

ARG PYTHON_VERSION
ARG BASE_IMAGE_DATE

LABEL org.metabrainz.based-on-image="metabrainz/python:${PYTHON_VERSION}"
LABEL org.opencontainers.image.source https://github.com/lidarr/sir

ARG DEBIAN_FRONTEND=noninteractive

#######################
# From metabrainz/sir #
#######################

RUN apt-get update \
    && apt-get install --no-install-recommends -qy \
      ca-certificates \
      cron \
      gcc \
      git \
      libc6-dev \
      # TODO: check if this is actually needed
      libffi-dev \
      # required for connections of pip packages
      libssl-dev \
      # required for psycopg2. Installs without it but links against a wrong .so.
      libpq-dev \
      # required by lxml from mb-rngpy
      libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

##################
# Installing sir #
##################

WORKDIR /code
COPY . /code/
RUN pip --disable-pip-version-check --no-cache-dir install -r requirements.txt \
    && rm -f /code/config.ini \
    && touch /etc/consul-template.conf \
    && mkdir sql

ENV POSTGRES_USER=musicbrainz
ENV POSTGRES_PASSWORD=musicbrainz
