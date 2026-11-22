import Link from "next/link";
import { notFound } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { fetchPublishedPostBySlug } from "@/lib/api";

export const dynamic = "force-dynamic";

interface PostPageProps {
  params: { slug: string };
}

function formatIssueDate(iso: string | null): string {
  if (!iso) return "Unscheduled";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "Unscheduled";
  return parsed.toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function estimateReadTimeMinutes(markdown: string): number {
  const words = markdown.trim().split(/\s+/).filter(Boolean).length;
  return Math.max(1, Math.ceil(words / 200));
}

export default async function PostDetailPage({ params }: PostPageProps) {
  const post = await fetchPublishedPostBySlug(params.slug);

  if (!post) {
    notFound();
  }

  const readTime = estimateReadTimeMinutes(post.markdown_body);

  return (
    <article className="space-y-10">
      <div>
        <Link
          href="/"
          className="text-sm font-medium text-stone-600 transition-colors hover:text-stone-950"
        >
          &larr; Back to all issues
        </Link>
      </div>

      <header className="space-y-4 border-b border-stone-200/90 pb-8">
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-xs text-stone-500">
          <time dateTime={post.published_at ?? undefined}>
            {formatIssueDate(post.published_at)}
          </time>
          <span aria-hidden="true">&middot;</span>
          <span>{readTime} min read</span>
        </div>

        <h1 className="font-serif text-3xl font-semibold leading-tight tracking-tight text-stone-950 sm:text-4xl">
          {post.title}
        </h1>

        {post.summary && (
          <p className="text-lg leading-relaxed text-stone-600">
            {post.summary}
          </p>
        )}
      </header>

      <div className="prose prose-stone prose-lg max-w-none prose-headings:font-serif prose-headings:font-semibold prose-headings:tracking-tight prose-a:text-stone-900 prose-a:decoration-stone-400 prose-a:underline-offset-4 hover:prose-a:decoration-stone-900 prose-blockquote:border-stone-300 prose-blockquote:text-stone-700 prose-code:rounded prose-code:bg-stone-200/70 prose-code:px-1.5 prose-code:py-0.5 prose-code:font-normal prose-code:before:content-none prose-code:after:content-none prose-pre:bg-stone-900 prose-pre:text-stone-100">
        <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
          {post.markdown_body}
        </ReactMarkdown>
      </div>

      <footer className="border-t border-stone-200/90 pt-8">
        <Link
          href="/"
          className="text-sm font-medium text-stone-600 transition-colors hover:text-stone-950"
        >
          &larr; Back to all issues
        </Link>
      </footer>
    </article>
  );
}
