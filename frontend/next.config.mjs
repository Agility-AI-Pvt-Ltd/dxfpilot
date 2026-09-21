/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Self-contained server for the Docker image (frontend/Dockerfile)
  output: "standalone",
};
export default nextConfig;
