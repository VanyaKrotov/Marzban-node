ARG PYTHON_VERSION=3.12
ARG ACME_SH_VERSION=3.1.1

FROM python:$PYTHON_VERSION-slim AS build
ARG ACME_SH_VERSION

ENV PYTHONUNBUFFERED=1

WORKDIR /code

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl unzip gcc python3-dev libpq-dev \
    && curl -fsSL https://raw.githubusercontent.com/VanyaKrotov/Marzban/master/scripts/install_latest_xray.sh | bash \
    && curl -fsSL "https://raw.githubusercontent.com/acmesh-official/acme.sh/${ACME_SH_VERSION}/acme.sh" \
        -o /usr/local/bin/acme.sh \
    && chmod 0755 /usr/local/bin/acme.sh \
    && rm -rf /var/lib/apt/lists/*

COPY ./requirements.txt /code/
RUN python3 -m pip install --upgrade pip setuptools \
    && pip install --no-cache-dir --upgrade -r /code/requirements.txt

FROM python:$PYTHON_VERSION-slim

ENV PYTHON_LIB_PATH=/usr/local/lib/python${PYTHON_VERSION%.*}/site-packages
WORKDIR /code

RUN rm -rf $PYTHON_LIB_PATH/* \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl openssl socat \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build $PYTHON_LIB_PATH $PYTHON_LIB_PATH
COPY --from=build /usr/local/bin /usr/local/bin
COPY --from=build /usr/local/share/xray /usr/local/share/xray

COPY . /code

CMD ["bash", "-c", "python main.py"]
