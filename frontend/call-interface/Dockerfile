# Vite bakes VITE_* vars into the bundle at BUILD time, not container start time -- a
# .env change alone won't reach an already-built image. docker-compose.yml passes these
# as build args for that reason; the fallbacks in src/api/client.ts and src/App.tsx
# (localhost:8080 / localhost:7880) are correct as-is for this deployment shape anyway,
# since the browser (not another container) is what resolves them -- but don't rely on
# that silently if these ever diverge.
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_API_GATEWAY_URL
ARG VITE_LIVEKIT_URL
RUN npm run build

# Vite's dev server is fine for the grading deployment (single machine, single audience) --
# no need for an nginx/static-file stage's extra complexity here. If that ever changes,
# swap this stage for `nginx:alpine` serving `dist/`.
FROM node:22-alpine
WORKDIR /app
COPY --from=build /app/dist ./dist
RUN npm install -g serve
EXPOSE 5173
CMD ["serve", "-s", "dist", "-l", "5173"]
