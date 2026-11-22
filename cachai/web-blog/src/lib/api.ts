export interface BlogPost {
  id: number;
  title: string;
  slug: string;
  summary: string;
  markdown_body: string;
  eval_id: number | null;
  status: "published";
  published_at: string | null;
}

function getApiBaseUrl(): string {
  return (
    process.env.INTERNAL_API_URL ||
    process.env.NEXT_PUBLIC_API_URL ||
    "http://localhost:8000"
  ).replace(/\/$/, "");
}

export async function fetchPublishedPosts(): Promise<BlogPost[]> {
  try {
    const res = await fetch(`${getApiBaseUrl()}/api/posts`, {
      cache: "no-store",
    });
    if (!res.ok) {
      return [];
    }
    const posts: BlogPost[] = await res.json();
    return posts.filter((post) => post.status === "published");
  } catch {
    return [];
  }
}

export async function fetchPublishedPostBySlug(
  slug: string
): Promise<BlogPost | null> {
  try {
    const res = await fetch(
      `${getApiBaseUrl()}/api/posts/${encodeURIComponent(slug)}`,
      { cache: "no-store" }
    );
    if (!res.ok) {
      return null;
    }
    const post: BlogPost = await res.json();
    return post.status === "published" ? post : null;
  } catch {
    return null;
  }
}
