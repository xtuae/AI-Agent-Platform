import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Bot, Hand, Search, Send, UserRound } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Textarea } from "@/components/ui/input";
import { Empty, ErrorNote, Spinner } from "@/components/ui/misc";
import { api, ApiError } from "@/lib/api";
import { useAuth, useCan } from "@/lib/auth";
import { ago, dateTime, ESCALATION_LABEL, label, phone, timeOnly } from "@/lib/format";
import { usePollInterval } from "@/lib/stream";
import type { ConversationRow, MessageOut, Page, Thread } from "@/lib/types";
import { cn } from "@/lib/utils";

const TABS = [
  { key: "", label: "Active" },
  { key: "human", label: "Needs a person" },
  { key: "live", label: "Last 24 h" },
] as const;

export default function ConversationsPage() {
  const { id } = useParams();
  return (
    <div className="md:grid md:h-[calc(100dvh-4rem)] md:grid-cols-[22rem_1fr] md:gap-4">
      <div className={cn("min-h-0", id && "hidden md:block")}>
        <Inbox selected={id} />
      </div>
      <div className={cn("min-h-0", !id && "hidden md:block")}>
        {id ? (
          <ThreadView id={id} />
        ) : (
          <Card className="hidden h-full items-center justify-center md:flex">
            <Empty title="Pick a conversation">Chats that need a person are listed first.</Empty>
          </Card>
        )}
      </div>
    </div>
  );
}

function Inbox({ selected }: { selected?: string }) {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "";
  const [q, setQ] = useState("");
  const query = {
    state: tab === "human" ? "awaiting_human" : undefined,
    live: tab === "live" ? true : undefined,
    q: q.trim() || undefined,
    limit: 100,
  };
  const list = useQuery({
    queryKey: ["conversations", query],
    queryFn: () => api<Page<ConversationRow>>("/conversations", { query }),
    placeholderData: keepPreviousData,
    refetchInterval: usePollInterval(),
  });

  return (
    <div className="flex h-full flex-col">
      <h1 className="mb-3 text-xl font-semibold tracking-tight">Chats</h1>
      <div className="mb-2 flex gap-2 overflow-x-auto pb-1" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => setParams(t.key ? { tab: t.key } : {}, { replace: true })}
            className={cn(
              "h-8 shrink-0 rounded-full border px-3 text-sm",
              tab === t.key ? "border-accent bg-accent/10 font-medium text-accent-ink" : "border-line bg-surface text-ink-2",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="relative mb-3">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
        <Input className="pl-9" placeholder="Name or phone" aria-label="Search chats" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <ErrorNote error={list.error} />
      <Card className="min-h-0 flex-1 overflow-y-auto">
        {list.isPending ? (
          <div className="flex justify-center py-10">
            <Spinner />
          </div>
        ) : !list.data?.items.length ? (
          <Empty title={tab === "human" ? "Nobody is waiting for a person" : "No conversations"} />
        ) : (
          <ul className="divide-y divide-line">
            {list.data.items.map((c) => (
              <li key={c.id}>
                <Link
                  to={`/conversations/${c.id}${tab ? `?tab=${tab}` : ""}`}
                  className={cn("block px-4 py-3 hover:bg-line/30", selected === c.id && "bg-accent/5")}
                  aria-current={selected === c.id ? "page" : undefined}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-medium">{c.customer.name ?? phone(c.customer.wa_id)}</span>
                    <span className="shrink-0 text-xs text-muted">{ago(c.last_message?.at ?? c.last_inbound_at)}</span>
                  </div>
                  <p className="mt-0.5 truncate text-sm text-ink-2" dir="auto">
                    {c.last_message?.direction === "out" ? "You: " : ""}
                    {c.last_message?.preview ?? "—"}
                  </p>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    {c.state === "awaiting_human" ? (
                      <Badge tone="warn">
                        <Hand className="size-3" aria-hidden />
                        {label(ESCALATION_LABEL, c.escalation?.reason ?? "taken_over")}
                        {c.assigned_to_name ? ` · ${c.assigned_to_name}` : ""}
                      </Badge>
                    ) : (
                      <Badge>
                        <Bot className="size-3" aria-hidden /> Agent
                      </Badge>
                    )}
                    {c.waiting ? <Badge tone="accent">{c.waiting} unanswered</Badge> : null}
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function ThreadView({ id }: { id: string }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { session } = useAuth();
  const canAct = useCan("agent");
  const isAdmin = useCan("admin");
  const bottom = useRef<HTMLDivElement>(null);
  const [text, setText] = useState("");

  const thread = useQuery({
    queryKey: ["thread", id],
    queryFn: () => api<Thread>(`/conversations/${id}`),
    refetchInterval: usePollInterval(),
  });
  const onDone = (t: Thread) => {
    client.setQueryData(["thread", id], t);
    void client.invalidateQueries({ queryKey: ["conversations"] });
    void client.invalidateQueries({ queryKey: ["today"] });
  };
  const takeover = useMutation({ mutationFn: () => api<Thread>(`/conversations/${id}/takeover`, { method: "POST" }), onSuccess: onDone });
  const handback = useMutation({ mutationFn: () => api<Thread>(`/conversations/${id}/handback`, { method: "POST" }), onSuccess: onDone });
  const send = useMutation({
    mutationFn: (body: string) => api<Thread>(`/conversations/${id}/messages`, { method: "POST", body: { text: body } }),
    onSuccess: (t) => {
      setText("");
      onDone(t);
    },
  });

  const count = thread.data?.messages.length ?? 0;
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [count, id]);

  const t = thread.data;
  if (thread.isError) {
    return (
      <div>
        <BackLink tab={params.get("tab")} />
        <ErrorNote error={thread.error} />
      </div>
    );
  }
  if (!t) {
    return (
      <div className="flex justify-center py-16">
        <Spinner />
      </div>
    );
  }

  const mine = t.assigned_to === session?.user.id;
  const human = t.state === "awaiting_human";
  const canReply = canAct && human && (mine || isAdmin);
  const error = takeover.error ?? handback.error ?? send.error;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (text.trim()) send.mutate(text.trim());
  }

  return (
    <Card className="flex h-[calc(100dvh-9rem)] flex-col md:h-full">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2.5">
        <Button variant="ghost" size="icon" className="md:hidden" aria-label="Back to chats" onClick={() => navigate(`/conversations${params.get("tab") ? `?tab=${params.get("tab")}` : ""}`)}>
          <ArrowLeft className="size-5" />
        </Button>
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{t.customer.name ?? phone(t.customer.wa_id)}</p>
          <p className="truncate text-xs text-muted">
            <a href={`tel:+${t.customer.wa_id}`}>{phone(t.customer.wa_id)}</a>
            {t.customer.area ? ` · ${t.customer.area}` : ""} ·{" "}
            {t.window_open ? `reply window open until ${timeOnly(t.window_expires_at ?? "")}` : "reply window closed"}
          </p>
        </div>
        {canAct ? (
          human ? (
            <div className="flex gap-2">
              {!t.assigned_to ? (
                // escalated by the agent, nobody has claimed it yet
                <Button size="sm" disabled={takeover.isPending} onClick={() => takeover.mutate()}>
                  <Hand /> Take it
                </Button>
              ) : null}
              <Button size="sm" variant="secondary" disabled={handback.isPending} onClick={() => handback.mutate()}>
                <Bot /> Hand back
              </Button>
            </div>
          ) : t.state !== "closed" ? (
            <Button size="sm" disabled={takeover.isPending} onClick={() => takeover.mutate()}>
              <Hand /> Take over
            </Button>
          ) : null
        ) : null}
      </div>

      {human ? (
        <div className="border-b border-line bg-warn/5 px-4 py-2 text-sm">
          <span className="font-medium text-warn">{label(ESCALATION_LABEL, t.escalation?.reason ?? "taken_over")}</span>
          <span className="text-ink-2">
            {" "}
            · the agent is paused{t.assigned_to_name ? ` · ${mine ? "you have it" : t.assigned_to_name}` : ""}
          </span>
          {t.escalation?.summary ? <p className="mt-0.5 text-ink-2" dir="auto">{t.escalation.summary}</p> : null}
          {human && !mine && !isAdmin && canAct ? (
            <p className="mt-0.5 text-xs text-muted">Only the person who took it over (or an admin) can reply.</p>
          ) : null}
        </div>
      ) : null}

      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-3 py-4">
        {t.summary ? (
          <details className="mx-auto mb-3 max-w-md rounded-lg border border-line px-3 py-2 text-xs text-ink-2">
            <summary className="cursor-pointer text-muted">Earlier conversation summary</summary>
            <p className="mt-1" dir="auto">{t.summary}</p>
          </details>
        ) : null}
        {t.messages.map((m) => (
          <Bubble key={m.id} m={m} />
        ))}
        <div ref={bottom} />
      </div>

      {error ? (
        <div className="px-3 pb-2">
          <ErrorNote error={explain(error)} />
        </div>
      ) : null}
      {canReply ? (
        t.window_open ? (
          <form onSubmit={submit} className="flex items-end gap-2 border-t border-line p-2">
            <Textarea
              dir="auto"
              rows={1}
              className="max-h-32 min-h-10 flex-1 resize-none"
              placeholder="Write a reply…"
              aria-label="Reply"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !("ontouchstart" in window)) {
                  e.preventDefault();
                  if (text.trim()) send.mutate(text.trim());
                }
              }}
            />
            <Button type="submit" size="icon" aria-label="Send" disabled={!text.trim() || send.isPending}>
              <Send />
            </Button>
          </form>
        ) : (
          <p className="border-t border-line px-4 py-3 text-sm text-muted">
            The customer last wrote over 24 hours ago, so WhatsApp only allows an approved template here (coming with campaigns).
            Call them instead: <a className="text-accent-ink" href={`tel:+${t.customer.wa_id}`}>{phone(t.customer.wa_id)}</a>
          </p>
        )
      ) : null}
    </Card>
  );
}

function BackLink({ tab }: { tab: string | null }) {
  return (
    <Link to={`/conversations${tab ? `?tab=${tab}` : ""}`} className="mb-3 inline-flex items-center gap-1 text-sm text-ink-2">
      <ArrowLeft className="size-4" /> Chats
    </Link>
  );
}

function explain(error: unknown): unknown {
  if (error instanceof ApiError) {
    const map: Record<string, string> = {
      take_over_first: "Take the conversation over before replying.",
      window_closed: "The 24-hour reply window has closed.",
      assigned_to_someone_else: "Someone else has this conversation.",
      whatsapp_rejected: "WhatsApp did not accept the message. Try again, or call the customer.",
      not_sendable: "This number is not set up to send yet. Contact HMH Labz.",
    };
    if (error.code && map[error.code]) return new Error(map[error.code]);
  }
  return error;
}

function Bubble({ m }: { m: MessageOut }) {
  const inbound = m.direction === "in";
  const text = m.transcript ?? m.body;
  const who =
    m.author === "person" ? (m.sent_by_name ?? "Team") : m.author === "agent" ? "Agent" : m.author === "template" ? `Template · ${m.template_name}` : null;
  return (
    <div className={cn("flex", inbound ? "justify-start" : "justify-end")}>
      <div
        className={cn(
          "max-w-[85%] rounded-2xl px-3 py-2 text-sm md:max-w-[70%]",
          inbound ? "rounded-bl-sm border border-line bg-surface" : m.author === "person" ? "rounded-br-sm bg-accent text-white" : "rounded-br-sm bg-accent/10",
        )}
      >
        {who ? (
          <p className={cn("mb-0.5 flex items-center gap-1 text-[11px]", m.author === "person" ? "text-white/80" : "text-muted")}>
            {m.author === "person" ? <UserRound className="size-3" aria-hidden /> : <Bot className="size-3" aria-hidden />} {who}
          </p>
        ) : null}
        {m.transcript ? <p className={cn("mb-0.5 text-[11px]", inbound ? "text-muted" : "")}>Voice note, transcribed</p> : null}
        <p className="whitespace-pre-wrap break-words" dir="auto">
          {text ?? `[${m.msg_type ?? "message"}]`}
        </p>
        <p className={cn("mt-0.5 text-right text-[11px]", m.author === "person" ? "text-white/80" : "text-muted")} title={dateTime(m.created_at)}>
          {timeOnly(m.created_at)}
          {!inbound && m.status ? ` · ${m.status}` : ""}
          {m.error_code ? ` · error ${m.error_code}` : ""}
        </p>
      </div>
    </div>
  );
}
