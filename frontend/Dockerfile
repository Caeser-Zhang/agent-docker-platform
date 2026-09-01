# Frontend Dockerfile — Browser Layer (React SPA)
FROM node:20-slim AS builder

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --registry https://registry.npmmirror.com --maxsockets=2 --no-audit --no-fund
# CACHEBUST: 值变化即令源码层失效（/mnt/d drvfs 上内容 hash 偶发误判缓存）。
# npm ci 层不受影响，仍然走缓存，重建只需几十秒。
ARG CACHEBUST=0
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
