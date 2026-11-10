ARG BASE_IMAGE=pic-api:local
FROM ${BASE_IMAGE}

ARG APP_RELEASE_SHA
LABEL org.opencontainers.image.revision=${APP_RELEASE_SHA}

COPY backend/tests/fixtures/database-rollback/016_p3_32_release_marker.sql \
    /app/migrations/016_p3_32_release_marker.sql

