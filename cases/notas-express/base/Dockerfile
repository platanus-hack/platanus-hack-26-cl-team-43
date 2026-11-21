FROM node:20-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
USER node
ENV PORT=3000
CMD ["node", "src/server.js"]
