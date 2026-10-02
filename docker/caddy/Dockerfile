# Caddy with two plugins the spec needs but stock caddy:2-alpine does not include:
#   caddy-dns/cloudflare  — DNS-01 ACME challenge; a wildcard cert (*.hmhagents.com) cannot be
#                           issued over HTTP-01.
#   mholt/caddy-ratelimit — the `rate_limit` directive for /api/v1/auth/* (not in core Caddy).
# The plugins track current Caddy: 2.8 stopped compiling against them (zapslog.HandlerOptions).
FROM caddy:2.10-builder-alpine AS builder
RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare \
    --with github.com/mholt/caddy-ratelimit

FROM caddy:2.10-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
