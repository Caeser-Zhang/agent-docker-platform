import {
  useEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type CSSProperties,
} from "react";
import { App as AntdApp, Button, Input, Modal, Segmented } from "antd";
import { createWish, submitOpinion, type FeedbackCategory } from "../api";
import { wishStyles } from "../wishStyles";
import { WishFormFields, type WishFormValue } from "./WishFormModal";

/**
 * 意见反馈弹窗（F2 / F3，设计文档 §7.2）。
 *
 * 内部是**两态状态机**（`phase: "form" | "guide"`）而不是两个弹窗——避免关闭
 * 再打开造成的焦点丢失与动画割裂：
 *   态 1「填写」：分类 Segmented + 内容 + 粘贴截图（bug 提交成功即关闭）
 *   态 2「引导」：仅 feature，提交成功后原地切换为「发布到心愿墙」引导
 *
 * 权限说明：本组件只走用户侧端点（POST /api/opinions、POST /api/wishes），
 * 不出现任何管理员能力；姓名/工号由服务端从登录态取，界面上不做输入（D25）。
 */

const MAX_IMAGES = 3;
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const ALLOWED_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
const MIN_CONTENT = 5;

const PLACEHOLDER: Record<FeedbackCategory, string> = {
  bug: "请尽量写清：\n1. 复现步骤\n2. 期望结果\n3. 实际结果\n4. 发生时间",
  feature: "希望增加什么能力？在什么场景下会用到？",
};

interface LocalImage {
  file: File;
  url: string;
  name: string;
}

export function OpinionFeedbackModal({
  open,
  onClose,
  onNavigate,
}: {
  open: boolean;
  onClose: () => void;
  /** 引导态发布成功后询问「去心愿墙看看」的跳转回调 */
  onNavigate?: (page: "wishes") => void;
}) {
  const { message, modal } = AntdApp.useApp();

  const [phase, setPhase] = useState<"form" | "guide">("form");
  const [category, setCategory] = useState<FeedbackCategory>("bug");
  const [content, setContent] = useState("");
  const [images, setImages] = useState<LocalImage[]>([]);
  const [submitting, setSubmitting] = useState(false);

  // 引导态：提交成功后拿到的反馈 id（自助转心愿要透传 source_feedback_id）
  const [feedbackId, setFeedbackId] = useState<number | null>(null);
  const [guide, setGuide] = useState<WishFormValue>({
    title: "",
    description: "",
    type: "other",
    status: "evaluating",
  });
  const [publishing, setPublishing] = useState(false);

  const taRef = useRef<any>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  // objectURL 生命周期（F20）：卸载时兜底 revoke，避免内存泄漏
  const imagesRef = useRef<LocalImage[]>([]);
  imagesRef.current = images;

  const revokeAll = (list: LocalImage[]) =>
    list.forEach((i) => URL.revokeObjectURL(i.url));

  const reset = () => {
    revokeAll(imagesRef.current);
    setPhase("form");
    setCategory("bug");
    setContent("");
    setImages([]);
    setSubmitting(false);
    setFeedbackId(null);
    setPublishing(false);
    setGuide({ title: "", description: "", type: "other", status: "evaluating" });
  };

  // 重新打开 → 全部字段重置（分类回到 Bug、截图清空）
  useEffect(() => {
    if (open) reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => () => revokeAll(imagesRef.current), []);

  // 打开后自动聚焦输入框（验证清单 §9.3）
  useEffect(() => {
    if (!open || phase !== "form") return;
    const t = setTimeout(() => taRef.current?.focus(), 60);
    return () => clearTimeout(t);
  }, [open, phase]);

  // --- 截图 ---------------------------------------------------------------

  const addImages = (files: File[]) => {
    const valid: File[] = [];
    let badType = 0;
    let tooBig = 0;
    for (const f of files) {
      if (!ALLOWED_TYPES.includes(f.type)) {
        badType++;
        continue;
      }
      if (f.size > MAX_IMAGE_BYTES) {
        tooBig++;
        continue;
      }
      valid.push(f);
    }
    if (badType) message.error("仅支持 png / jpg / webp / gif");
    if (tooBig) message.error("图片超过 5MB，请压缩后重试");

    const room = MAX_IMAGES - images.length;
    if (room <= 0) {
      if (valid.length) message.warning("最多 3 张截图，已忽略多余的");
      return;
    }
    let taken = valid;
    if (valid.length > room) {
      taken = valid.slice(0, room);
      message.warning("最多 3 张截图，已忽略多余的");
    }
    if (!taken.length) return;
    // 本地立即预览（不等上传）；真正上传发生在点「提交」时
    setImages((prev) => [
      ...prev,
      ...taken.map((f) => ({ file: f, url: URL.createObjectURL(f), name: f.name })),
    ]);
  };

  const removeImage = (url: string) => {
    setImages((prev) => {
      const target = prev.find((i) => i.url === url);
      if (target) URL.revokeObjectURL(target.url);
      return prev.filter((i) => i.url !== url);
    });
  };

  /** Ctrl+V 粘贴截图；纯文本粘贴不拦截，走默认行为插入文字（§7.2）。 */
  const handlePaste = (e: ClipboardEvent) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (const it of items) {
      if (it.kind === "file" && it.type.startsWith("image/")) {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (!files.length) return;
    e.preventDefault(); // 阻止图片被当作富文本插入 textarea
    addImages(files);
  };

  // --- 提交（态 1） -------------------------------------------------------

  const trimmed = content.trim();
  const canSubmit = !submitting && trimmed.length >= MIN_CONTENT;

  const requestClose = () => {
    // 态 1 有内容/截图时二次确认，防误关丢失；态 2 无需确认（反馈已落库）
    if (phase === "form" && (trimmed.length > 0 || images.length > 0)) {
      modal.confirm({
        title: "放弃这条反馈？",
        content: "关闭后已填写的内容与截图将不会保留。",
        okText: "放弃",
        cancelText: "继续填写",
        okButtonProps: { danger: true },
        onOk: () => {
          reset();
          onClose();
        },
      });
      return;
    }
    reset();
    onClose();
  };

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const res = await submitOpinion({
        category,
        content: trimmed,
        images: images.map((i) => i.file),
      });
      if (category === "bug") {
        // D31：不做承诺性文案；D13：立即关闭，不进引导态
        message.success("已记录，感谢反馈");
        reset();
        onClose();
        return;
      }
      // feature → 原地切到引导态（D30①），弹窗不关闭
      message.success("感谢你的建议！");
      setFeedbackId(res.id);
      setGuide({
        title: trimmed.split("\n")[0].slice(0, 30),
        description: trimmed,
        type: "other",
        status: "evaluating",
      });
      revokeAll(images);
      setImages([]);
      setPhase("guide");
    } catch (e) {
      // 失败（429 / 422 / 413 / 网络）→ 保持态 1，内容与截图均不清空
      const err = e as Error & { status?: number };
      if (err.status === 429) message.warning(err.message);
      else message.error(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  // --- 去发布（态 2） -----------------------------------------------------

  const handlePublish = async () => {
    if (!guide.title.trim() || publishing) return;
    setPublishing(true);
    try {
      await createWish({
        title: guide.title.trim(),
        description: guide.description.trim(),
        type: guide.type,
        source_feedback_id: feedbackId ?? undefined,
      });
      message.success("已发布到心愿墙");
      reset();
      onClose();
      modal.confirm({
        title: "去心愿墙看看？",
        content: "其他用户可以在心愿墙看到并助力这条心愿。",
        okText: "去看看",
        cancelText: "留在当前页",
        onOk: () => onNavigate?.("wishes"),
      });
    } catch (e) {
      const err = e as Error & { status?: number };
      if (err.status === 409) {
        // 可能是管理员抢先转化（§6.8）：提示后关闭引导态，不重复建心愿
        message.warning("该反馈已转化为心愿");
        reset();
        onClose();
      } else if (err.status === 429) {
        message.warning(err.message); // 引导态保持，用户可稍后再点
      } else {
        message.error(err.message); // 引导态保持，预填内容不丢
      }
    } finally {
      setPublishing(false);
    }
  };

  // --- 渲染 ---------------------------------------------------------------

  const formTitle = (
    <div>
      <div>意见反馈</div>
      <div style={{ fontSize: 12, fontWeight: 400, color: "var(--text-3)" }}>
        我们会认真阅读每一条反馈
      </div>
    </div>
  );

  return (
    <Modal
      open={open}
      width={560}
      title={phase === "form" ? formTitle : "要不要把它发布到心愿墙？"}
      onCancel={submitting || publishing ? undefined : requestClose}
      maskClosable={false}
      footer={
        phase === "form"
          ? [
              <Button key="cancel" onClick={requestClose} disabled={submitting}>
                取消
              </Button>,
              <Button
                key="submit"
                type="primary"
                loading={submitting}
                disabled={!canSubmit}
                onClick={handleSubmit}
              >
                提交
              </Button>,
            ]
          : [
              <Button key="later" onClick={requestClose} disabled={publishing}>
                稍后再说
              </Button>,
              <Button
                key="publish"
                type="primary"
                loading={publishing}
                disabled={!guide.title.trim()}
                onClick={handlePublish}
              >
                去发布
              </Button>,
            ]
      }
    >
      {phase === "form" ? (
        <>
          <Segmented<FeedbackCategory>
            value={category}
            // D29：默认停在 Bug（积压最需要被先看到）
            options={[
              { value: "bug", label: "🐞 遇到问题(Bug)" },
              { value: "feature", label: "✨ 功能建议" },
            ]}
            onChange={(v) => setCategory(v)}
            disabled={submitting}
            block
          />
          <div style={{ height: 12 }} />
          <Input.TextArea
            ref={taRef}
            value={content}
            rows={6}
            showCount
            maxLength={2000}
            disabled={submitting}
            placeholder={PLACEHOLDER[category]}
            onChange={(e) => setContent(e.target.value)}
            onPaste={handlePaste}
          />
          {trimmed.length > 0 && trimmed.length < MIN_CONTENT && (
            <div style={wishStyles.formError}>请至少输入 5 个字</div>
          )}
          {category === "bug" && (
            // 提示语只在 Bug 下显示；截图能力对两种分类都开放（§7.2）
            <div style={wishStyles.formHint}>可 Ctrl+V 直接粘贴截图（最多 3 张）</div>
          )}

          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 12 }}>
            {images.map((img) => (
              <div key={img.url} style={thumbBox}>
                <img src={img.url} alt={img.name} style={thumbImg} />
                <button
                  type="button"
                  style={thumbRemove}
                  title="移除这张截图"
                  onClick={() => removeImage(img.url)}
                  disabled={submitting}
                >
                  ✕
                </button>
              </div>
            ))}
            {images.length < MAX_IMAGES && (
              <button
                type="button"
                style={thumbAdd}
                onClick={() => fileRef.current?.click()}
                disabled={submitting}
              >
                ＋ 添加
              </button>
            )}
          </div>
          <input
            ref={fileRef}
            type="file"
            accept={ALLOWED_TYPES.join(",")}
            multiple
            style={{ display: "none" }}
            onChange={(e) => {
              addImages(Array.from(e.target.files ?? []));
              e.target.value = ""; // 允许重复选择同一文件
            }}
          />
        </>
      ) : (
        <>
          <div style={wishStyles.guideNote}>
            发布后其他用户可以看到并助力，我们会按助力数评估优先级。
          </div>
          <WishFormFields
            value={guide}
            // 引导态是用户自助通道：不渲染状态下拉（状态由服务端固定为「评估中」）
            showStatus={false}
            onChange={(p) => setGuide((v) => ({ ...v, ...p }))}
          />
          <div style={wishStyles.formHint}>
            也可以稍后再说——反馈已经提交，管理员同样可以在后台把它转为心愿。
          </div>
        </>
      )}
    </Modal>
  );
}

// 截图缩略图（本地 objectURL 预览，不等上传）
const thumbBox: CSSProperties = {
  position: "relative",
  width: 72,
  height: 72,
  borderRadius: 8,
  overflow: "hidden",
  border: "1px solid var(--border)",
  background: "var(--surface-2)",
};
const thumbImg: CSSProperties = {
  width: "100%",
  height: "100%",
  objectFit: "cover",
  display: "block",
};
const thumbRemove: CSSProperties = {
  position: "absolute",
  top: 2,
  right: 2,
  width: 18,
  height: 18,
  lineHeight: "16px",
  padding: 0,
  fontSize: 11,
  borderRadius: "50%",
  border: "none",
  cursor: "pointer",
  background: "rgba(0,0,0,0.6)",
  color: "#fff",
};
const thumbAdd: CSSProperties = {
  width: 72,
  height: 72,
  borderRadius: 8,
  border: "1px dashed var(--border-strong)",
  background: "transparent",
  color: "var(--text-3)",
  fontSize: 12,
  cursor: "pointer",
};
