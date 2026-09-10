# =============================================================================
# LogSherlock frontend — build with Node, serve with nginx
# =============================================================================
# Multi-stage, and the split is the whole point: the output of `npm run build`
# is static files. Shipping the Node runtime and `node_modules` alongside them
# would multiply the image size for a toolchain nothing at run time uses, and
# would put a package manager and a compiler inside a container whose only job
# is to answer GETs.
# =============================================================================

# -----------------------------------------------------------------------------
# build — type-check, bundle
# -----------------------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app/frontend

# The lockfile layer, so editing a component does not reinstall the toolchain.
# `npm ci` rather than `npm install`: it installs exactly what
# `package-lock.json` pins and fails if the two disagree, which is the property
# that makes an image build reproducible.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Where the client sends its requests, baked in at build time because Vite
# resolves `import.meta.env` during the bundle — there is no run-time
# configuration for a file already served to a browser.
#
# **Empty is the correct default here**, and it is not an omission. An unset
# value makes `services/api.ts` fall back to the relative path `/api`, which
# `nginx.conf` proxies to the backend service: every request is then
# same-origin, no CORS is involved, and the bundle does not hardcode a host
# port that only happens to be right on the machine that built it. Override it
# only for a deployment that genuinely serves the two from different origins:
#
#     docker compose build --build-arg VITE_API_BASE_URL=https://api.example.com frontend
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

# `tsc -b && vite build` — a real type-check, not just a bundle, so a type error
# fails the image build rather than shipping. Note `vite.config.ts` points
# `envDir` at the repository root, which is outside this build context: there is
# therefore no `.env` to find here, the config logs that it found none, and the
# value above is the only source of `VITE_API_BASE_URL`. That is deliberate —
# a developer's `.env` must not be able to leak into an image.
RUN npm run build

# -----------------------------------------------------------------------------
# serve — nginx over the static bundle
# -----------------------------------------------------------------------------
FROM nginx:1.27-alpine AS serve

# Replaces the default server block rather than adding a second one, so there is
# no chance of the stock config winning on `/`.
COPY nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=build /app/frontend/dist /usr/share/nginx/html

EXPOSE 80

# The base image's own entrypoint runs the template processor and starts nginx
# in the foreground, which is what a container needs — so no CMD of our own.
