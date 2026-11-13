/** Proxy /api/* to the FastAPI service so the browser never needs CORS. */
const API_URL = process.env.LEDGER_API_URL || "http://127.0.0.1:8000";

module.exports = {
  reactStrictMode: true,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/:path*` }];
  },
};
