# -----------------------------
# FRONTEND BUILD STAGE
# -----------------------------
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS frontend-build
WORKDIR /build
COPY src/frontend/package*.json ./
RUN npm ci
COPY src/frontend/ ./
RUN npm run build

FROM python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
RUN apk add --no-cache bash curl docker-cli docker-cli-compose tar openssl \
    && apk add --no-cache --virtual .build-deps gcc musl-dev libffi-dev
WORKDIR /app
COPY src/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && apk del .build-deps
COPY src/app /app/app
COPY --from=frontend-build /build/dist /app/static
COPY src/generateenv.sh src/env.defaults /app/
COPY build/scripts/start-prod.sh /app/start-prod.sh
RUN sed -i 's/\r$//' /app/*.sh /app/env.defaults && chmod +x /app/*.sh
ENV PYTHONUNBUFFERED=1
EXPOSE 2999
CMD ["sh", "/app/start-prod.sh"]
