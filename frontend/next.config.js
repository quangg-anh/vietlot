const api = process.env.NEXT_PUBLIC_API_BASE || "/api";
module.exports = {
  async rewrites() {
    if (api.startsWith("http")) return [];
    return [{ source: "/api/:path*", destination: "http://api:8000/api/:path*" }];
  }
};
