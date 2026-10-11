FROM ghcr.io/anomalyco/opencode:latest

# Keep GitHub CLI in the image so it is available after container recreation.
# Docker may inject proxy build arguments even though the Compose service clears them.
RUN env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY \
        -u http_proxy -u https_proxy -u all_proxy -u no_proxy \
    apk add --no-cache github-cli \
    && gh --version
