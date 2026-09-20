import { useEffect, useState } from "react";
import { App as AntdApp, Button, Input, Modal, Select } from "antd";
import {
  createWish,
  patchWish,
  type WishItem,
  type WishStatus,
  type WishType,
} from "../api";
import { WISH_STATUS_OPTIONS, WISH_TYPE_OPTIONS, wishStyles } from "../wishStyles";

/** 心愿表单的字段值。status 仅管理员编辑时参与提交（权限区分，§5.2）。 */
export interface WishFormValue {
  title: string;
  description: string;
  type: WishType;
  status: WishStatus;
}

/**
 * 心愿表单字段（无 Modal 外壳）。
 *
 * 单独抽出是为了让「意见反馈弹窗的转心愿引导态」（§7.2 态 2）复用同一份表单，
 * 而不是再写一遍 title/description/type 三个控件（§7.3）。
 * `showStatus` 由调用方按角色传入——普通用户/作者永远看不到状态下拉。
 */
export function WishFormFields({
  value,
  onChange,
  showStatus,
}: {
  value: WishFormValue;
  onChange: (patch: Partial<WishFormValue>) => void;
  showStatus?: boolean;
}) {
  return (
    <>
      <div style={wishStyles.field}>
        <label style={wishStyles.fieldLabel}>心愿名称</label>
        <Input
          value={value.title}
          maxLength={120}
          showCount
          placeholder="一句话说明你想要什么"
          onChange={(e) => onChange({ title: e.target.value })}
        />
      </div>
      <div style={wishStyles.field}>
        <label style={wishStyles.fieldLabel}>详细描述</label>
        <Input.TextArea
          value={value.description}
          rows={5}
          maxLength={5000}
          showCount
          placeholder="在什么场景下会用到？希望解决什么问题？"
          onChange={(e) => onChange({ description: e.target.value })}
        />
      </div>
      <div style={wishStyles.field}>
        <label style={wishStyles.fieldLabel}>类型</label>
        <Select<WishType>
          value={value.type}
          options={WISH_TYPE_OPTIONS}
          style={{ width: "100%" }}
          onChange={(v) => onChange({ type: v })}
        />
      </div>
      {showStatus && (
        <div style={wishStyles.field}>
          <label style={wishStyles.fieldLabel}>状态（仅管理员可修改）</label>
          <Select<WishStatus>
            value={value.status}
            options={WISH_STATUS_OPTIONS}
            style={{ width: "100%" }}
            onChange={(v) => onChange({ status: v })}
          />
        </div>
      )}
    </>
  );
}

/**
 * 发布 / 编辑心愿弹窗（F7）。
 *
 * - `mode="create"`：可接受外部预填（`initial`）并透传 `sourceFeedbackId`，
 *   供后台「转为心愿」与自助通道复用。
 * - `mode="edit"`：作者可改名称/描述/类型；`isAdmin` 为真时才渲染状态下拉，
 *   且仅在状态实际变化时把 `status` 放进 PATCH body——避免普通作者误触后端 403。
 */
export function WishFormModal({
  open,
  mode,
  wish,
  initial,
  sourceFeedbackId,
  isAdmin,
  onCancel,
  onDone,
}: {
  open: boolean;
  mode: "create" | "edit";
  /** edit 模式的目标心愿 */
  wish?: WishItem | null;
  /** create 模式的预填值 */
  initial?: { title?: string; description?: string; type?: WishType } | null;
  /** create 模式下透传的自助转心愿来源（§6.8） */
  sourceFeedbackId?: number | null;
  isAdmin: boolean;
  onCancel: () => void;
  onDone: (w: WishItem) => void;
}) {
  const { message } = AntdApp.useApp();
  const [value, setValue] = useState<WishFormValue>({
    title: "",
    description: "",
    type: "other",
    status: "evaluating",
  });
  const [busy, setBusy] = useState(false);

  // 每次打开都重置为「既有值 / 外部预填」，避免上一次编辑的残留串场。
  useEffect(() => {
    if (!open) return;
    setBusy(false);
    if (mode === "edit" && wish) {
      setValue({
        title: wish.title,
        description: wish.description,
        type: wish.type,
        status: wish.status,
      });
    } else {
      setValue({
        title: initial?.title ?? "",
        description: initial?.description ?? "",
        type: initial?.type ?? "other",
        status: "evaluating",
      });
    }
  }, [open, mode, wish, initial]);

  const patch = (p: Partial<WishFormValue>) => setValue((v) => ({ ...v, ...p }));
  const canSubmit = !busy && value.title.trim().length > 0;

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    try {
      if (mode === "create") {
        const w = await createWish({
          title: value.title.trim(),
          description: value.description.trim(),
          type: value.type,
          source_feedback_id: sourceFeedbackId ?? undefined,
        });
        message.success("已发布到心愿墙");
        onDone(w);
        return;
      }
      if (!wish) return;
      const body: Partial<{
        title: string;
        description: string;
        type: WishType;
        status: WishStatus;
      }> = {
        title: value.title.trim(),
        description: value.description.trim(),
        type: value.type,
      };
      // 权限区分：status 只有管理员能改，普通作者不带该字段（后端会 403）。
      if (isAdmin && value.status !== wish.status) body.status = value.status;
      const w = await patchWish(wish.id, body);
      message.success("已保存");
      onDone(w);
    } catch (e) {
      const err = e as Error & { status?: number };
      // 429 = 发布限流（3 次/60s）；403 = 越权（如作者试图改状态）。
      if (err.status === 429 || err.status === 403) message.warning(err.message);
      else message.error(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title={mode === "create" ? "发布心愿" : "编辑心愿"}
      onCancel={busy ? undefined : onCancel}
      maskClosable={false}
      destroyOnHidden
      footer={[
        <Button key="cancel" onClick={onCancel} disabled={busy}>
          取消
        </Button>,
        <Button key="ok" type="primary" loading={busy} disabled={!canSubmit} onClick={submit}>
          {mode === "create" ? "发布" : "保存"}
        </Button>,
      ]}
    >
      <WishFormFields
        value={value}
        onChange={patch}
        showStatus={mode === "edit" && isAdmin}
      />
      {mode === "create" && (
        <div style={wishStyles.formHint}>
          发布后其他用户可以看到并助力，我们会按助力数评估优先级。
        </div>
      )}
    </Modal>
  );
}
