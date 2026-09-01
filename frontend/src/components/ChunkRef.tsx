/**
 * fastk chunk 引用：把 assistant 消息文本中的规范化引用标记
 * `[[chunk:<db>/<chunk_id>]]` 渲染为可点击徽章；点击后经平台代理取回该
 * chunk 的完整内容（含图片等富媒体）并以模态展示。
 *
 * 中文显示优化集中在查看器样式（chatStyles.chunkViewerBody）：中文字体栈、
 * 1.75 行高、两端对齐、strict 禁则（避免句号/逗号悬挂行首）。
 */

import { useEffect, useState } from "react";
import { api, type FastkChunk } from "../api";
import { styles } from "./chatStyles";

/** 引用标记：`[[chunk:fastdb/fb62184133c0c818]]`。库名与 chunk_id 均为受限字符集。 */
const CHUNK_REF_RE = /\[\[chunk:([A-Za-z0-9_-]{1,64})\/([A-Za-z0-9_-]{1,64})\]\]/g;

type Segment = { kind: "text"; text: string } | { kind: "ref"; db: string; chunkId: string };

/** 把含引用标记的文本切分为纯文本段与引用段（供行内渲染）。 */
export function parseChunkRefs(text: string): Segment[] {
  const segments: Segment[] = [];
  let last = 0;
  for (const m of text.matchAll(CHUNK_REF_RE)) {
    const i = m.index ?? 0;
    if (i > last) segments.push({ kind: "text", text: text.slice(last, i) });
    segments.push({ kind: "ref", db: m[1], chunkId: m[2] });
    last = i + m[0].length;
  }
  if (last < text.length) segments.push({ kind: "text", text: text.slice(last) });
  return segments;
}

/** 单个 chunk 引用徽章（行内）。 */
function ChunkBadge({ db, chunkId, onOpen }: { db: string; chunkId: string; onOpen: () => void }) {
  return (
    <button
      type="button"
      style={styles.chunkBadge}
      title={`查看知识库引用 · ${db}/${chunkId}`}
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
    >
      <span aria-hidden>📚</span>
      <span>引用</span>
    </button>
  );
}

/** 引用内容查看器：模态展示 chunk 元数据、正文与附图。 */
function ChunkViewer({ db, chunkId, onClose }: { db: string; chunkId: string; onClose: () => void }) {
  const [chunk, setChunk] = useState<FastkChunk | null>(null);
  const [error, setError] = useState("");
  const [imageUrl, setImageUrl] = useState<string | null>(null);

  // 取 chunk 内容；随后若有附图再取图片 Blob（<img> 无法带鉴权头）。
  useEffect(() => {
    let alive = true;
    let objectUrl: string | null = null;
    setChunk(null);
    setError("");
    setImageUrl(null);
    api
      .getFastkChunk(db, chunkId)
      .then(async (c) => {
        if (!alive) return;
        setChunk(c);
        if (c.image_url) {
          try {
            const blob = await api.fetchFastkChunkImage(db, chunkId);
            objectUrl = URL.createObjectURL(blob);
            if (alive) setImageUrl(objectUrl);
            else URL.revokeObjectURL(objectUrl);
          } catch (e) {
            // 图片失败不阻断正文展示
            if (alive) setError(e instanceof Error ? e.message : "图片加载失败");
          }
        }
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : "加载失败");
      });
    return () => {
      alive = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [db, chunkId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div style={styles.modal} onClick={onClose} role="dialog" aria-modal>
      <div style={styles.modalContent} onClick={(e) => e.stopPropagation()}>
        <div style={styles.modalHeader}>
          <span>📚 知识库引用</span>
          <button type="button" style={styles.modalClose} onClick={onClose} aria-label="关闭">
            ×
          </button>
        </div>
        <div style={styles.chunkViewerBody}>
          {error && !chunk && <div style={styles.chunkError}>{error}</div>}
          {!chunk && !error && <div style={styles.chunkLoading}>正在加载引用内容…</div>}
          {chunk && (
            <>
              <div style={styles.chunkMeta}>
                <div style={styles.chunkMetaRow}>
                  <span style={styles.chunkMetaLabel}>来源文件：</span>
                  <span style={styles.chunkMetaValue}>{chunk.path || "（无）"}</span>
                </div>
                <div style={styles.chunkMetaRow}>
                  <span style={styles.chunkMetaLabel}>所在章节：</span>
                  <span style={styles.chunkMetaValue}>{chunk.section || "（无）"}</span>
                </div>
                <div style={styles.chunkMetaRow}>
                  <span style={styles.chunkMetaLabel}>知识库：</span>
                  <span style={styles.chunkMetaValue}>
                    {db} · 第 {chunk.chunk_index + 1} 块
                  </span>
                </div>
              </div>
              <div style={styles.chunkText}>{chunk.text}</div>
              {imageUrl && <img src={imageUrl} alt="知识库附图" style={styles.chunkImage} />}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * assistant 文本块渲染：解析 `[[chunk:...]]` 标记为行内徽章。同时管理
 * "当前打开的引用查看器"（同一时刻最多一个模态）。
 */
export function TextWithChunkRefs({ text, cursor }: { text: string; cursor?: boolean }) {
  const [openRef, setOpenRef] = useState<{ db: string; chunkId: string } | null>(null);
  const segments = parseChunkRefs(text);
  const hasRefs = segments.some((s) => s.kind === "ref");
  if (!hasRefs) {
    return (
      <div style={styles.msgText}>
        {text}
        {cursor && <span style={styles.cursor}>▊</span>}
      </div>
    );
  }
  return (
    <div style={styles.msgText}>
      {segments.map((seg, i) =>
        seg.kind === "text" ? (
          <span key={i}>{seg.text}</span>
        ) : (
          <ChunkBadge
            key={i}
            db={seg.db}
            chunkId={seg.chunkId}
            onOpen={() => setOpenRef({ db: seg.db, chunkId: seg.chunkId })}
          />
        )
      )}
      {cursor && <span style={styles.cursor}>▊</span>}
      {openRef && (
        <ChunkViewer
          db={openRef.db}
          chunkId={openRef.chunkId}
          onClose={() => setOpenRef(null)}
        />
      )}
    </div>
  );
}
