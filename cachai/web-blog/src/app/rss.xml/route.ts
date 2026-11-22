import { NextRequest } from "next/server";
import { BlogPost, fetchPublishedPosts } from "@/lib/api";

export const dynamic = "force-dynamic";

const INVALID_XML_CONTROL_CHARS =
  /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u0084\u0086-\u009F\uFFFE\uFFFF]/g;

function escapeXml(value: string): string {
  return value
    .replace(INVALID_XML_CONTROL_CHARS, "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function getSiteUrl(request: NextRequest): string {
  const configured = process.env.NEXT_PUBLIC_SITE_URL?.trim();
  if (configured) {
    return configured.replace(/\/+$/, "");
  }

  if (request.nextUrl?.origin && request.nextUrl.origin !== "null") {
    return request.nextUrl.origin.replace(/\/+$/, "");
  }

  try {
    const origin = new URL(request.url).origin;
    if (origin && origin !== "null") {
      return origin.replace(/\/+$/, "");
    }
  } catch {
    // Fall back to default local origin below.
  }

  return "http://localhost:3000";
}

function parseTimestamp(value: string | null): number {
  if (!value) {
    return 0;
  }
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? 0 : ms;
}

function formatRfc2822Date(value: string | null): string | null {
  if (!value) {
    return null;
  }
  const ms = Date.parse(value);
  if (Number.isNaN(ms)) {
    return null;
  }
  return new Date(ms).toUTCString();
}

function buildExcerpt(markdown: string, maxLength = 280): string {
  const plain = markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^[>\-*+]\s+/gm, "")
    .replace(/[*_~]+/g, "")
    .replace(/\s+/g, " ")
    .trim();

  if (plain.length <= maxLength) {
    return plain;
  }

  return `${plain.slice(0, maxLength - 3).trimEnd()}...`;
}

function buildItemXml(post: BlogPost, siteUrl: string): string {
  const itemUrl = `${siteUrl}/posts/${encodeURIComponent(post.slug)}`;
  const description = post.summary?.trim() || buildExcerpt(post.markdown_body || "");
  const pubDate = formatRfc2822Date(post.published_at);

  const lines = [
    "    <item>",
    `      <title>${escapeXml(post.title)}</title>`,
    `      <link>${escapeXml(itemUrl)}</link>`,
    `      <guid isPermaLink="true">${escapeXml(itemUrl)}</guid>`,
  ];

  if (pubDate) {
    lines.push(`      <pubDate>${escapeXml(pubDate)}</pubDate>`);
  }

  lines.push(
    `      <description>${escapeXml(description)}</description>`,
    "    </item>"
  );

  return lines.join("\n");
}

export async function GET(request: NextRequest): Promise<Response> {
  const posts = await fetchPublishedPosts();
  const sortedPosts = [...posts].sort((a, b) => {
    const diff = parseTimestamp(b.published_at) - parseTimestamp(a.published_at);
    if (diff !== 0) {
      return diff;
    }
    return b.id - a.id;
  });

  const siteUrl = getSiteUrl(request);
  const feedUrl = `${siteUrl}/rss.xml`;
  const lastBuildDate = new Date().toUTCString();
  const itemsXml = sortedPosts
    .map((post) => buildItemXml(post, siteUrl))
    .join("\n");

  const channelLines = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
    "  <channel>",
    "    <title>CachAI Dispatch</title>",
    `    <link>${escapeXml(siteUrl)}</link>`,
    "    <description>Autonomous LLM Security Briefings</description>",
    "    <language>en-us</language>",
    `    <lastBuildDate>${escapeXml(lastBuildDate)}</lastBuildDate>`,
    `    <atom:link href="${escapeXml(feedUrl)}" rel="self" type="application/rss+xml" />`,
  ];

  if (itemsXml) {
    channelLines.push(itemsXml);
  }

  channelLines.push("  </channel>", "</rss>", "");

  return new Response(channelLines.join("\n"), {
    status: 200,
    headers: {
      "Content-Type": "application/rss+xml; charset=utf-8",
      "Cache-Control": "no-store, max-age=0",
    },
  });
}
