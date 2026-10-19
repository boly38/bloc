# BLOC is qualified with OpenCode v1.19.3; qualify a new version before updating the digest.
ARG OPENCODE_IMAGE=ghcr.io/anomalyco/opencode@sha256:6ebc13097f2381301220abe9d1b2a54f50cd1603a910dc1c66e93d8f2c4e8ebb
FROM ${OPENCODE_IMAGE}

# Keep GitHub CLI in the image so it is available after container recreation.
# Docker may inject proxy build arguments even though the Compose service clears them.
RUN env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY \
        -u http_proxy -u https_proxy -u all_proxy -u no_proxy \
    apk add --no-cache github-cli \
    && gh --version
