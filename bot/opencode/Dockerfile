# BLOC 2.x is being qualified against OpenCode v2.1.4; change only when retargeting qualification.
ARG OPENCODE_IMAGE=ghcr.io/anomalyco/opencode@sha256:6176a71bf7c1352d9aa9e7f112a4ef38443079a2f435789a33dd5ae809acea1a
FROM ${OPENCODE_IMAGE}

# Keep GitHub CLI in the image so it is available after container recreation.
# Docker may inject proxy build arguments even though the Compose service clears them.
RUN env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY \
        -u http_proxy -u https_proxy -u all_proxy -u no_proxy \
    apk add --no-cache github-cli \
    && gh --version
