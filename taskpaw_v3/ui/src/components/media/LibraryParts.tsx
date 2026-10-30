// Small parts of 片库与规则 (#361 §11): the switch, the 3:4 folder.jpg thumbnail and the drawer shell.
import CloseIcon from "@mui/icons-material/Close";
import PersonAddAlt1OutlinedIcon from "@mui/icons-material/PersonAddAlt1Outlined";
import { Drawer } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, type ReactNode } from "react";
import { api } from "../../api";
import { Icon } from "./parts";
import { THUMB_KEY } from "./libraryModel";

export function Toggle({ checked, onChange, label, labelledBy, disabled }: {
  checked: boolean; onChange: (next: boolean) => void; label?: string; labelledBy?: string; disabled?: boolean;
}) {
  return (
    <button type="button" className="toggle" role="switch" aria-checked={checked} aria-label={label}
      aria-labelledby={labelledBy} disabled={disabled} onClick={() => onChange(!checked)} />
  );
}

// D5: the actor's `folder.jpg` (an authenticated image), portrait 3:4 — 54×72 in lists, 144×192 large.
export function ActorThumb({ id, hasThumb, name, large = false }: { id: string; hasThumb: boolean; name: string; large?: boolean }) {
  const image = useQuery({
    queryKey: [...THUMB_KEY, id], queryFn: () => api.image(api.thumbPath(id)),
    enabled: hasThumb, staleTime: Infinity, retry: false,
  });
  const cls = `avatar${large ? " lg" : ""}`;
  if (!hasThumb || !image.data) {
    return (
      <div className={`${cls} none`} role="img" aria-label={`${name}：没有头像`}>
        <Icon of={PersonAddAlt1OutlinedIcon} /><span>目录里没有 folder.jpg</span>
      </div>
    );
  }
  return <div className={cls}><img src={image.data} alt={`${name} 的 folder.jpg`} /></div>;
}

const BACKDROP = { sx: { backgroundColor: "#030812BA", backdropFilter: "blur(4px)" } };

// A right drawer like 影片详情: MUI keeps the focus inside, Escape closes, the title takes focus.
export function SideDrawer({ open, onClose, title, subtitle, label, children }: {
  open: boolean; onClose: () => void; title: string; subtitle: ReactNode; label: string; children: ReactNode;
}) {
  return (
    <Drawer anchor="right" open={open} onClose={onClose} ModalProps={{ disableAutoFocus: true }} slotProps={{ backdrop: BACKDROP }}
      PaperProps={{ className: "mw mw-drawer", sx: { backgroundImage: "none" }, role: "dialog", "aria-modal": true, "aria-label": label } as object}>
      {open && <DrawerHead title={title} subtitle={subtitle} onClose={onClose} />}
      {open && <div className="modal-body drawer-stack">{children}</div>}
    </Drawer>
  );
}

function DrawerHead({ title, subtitle, onClose }: { title: string; subtitle: ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => { ref.current?.focus({ preventScroll: true }); }, []);
  return (
    <div className="modal-head">
      <div><h2 tabIndex={-1} ref={ref}>{title}</h2><p>{subtitle}</p></div>
      <button type="button" className="btn ghost icon-btn" aria-label="关闭" onClick={onClose}><Icon of={CloseIcon} /></button>
    </div>
  );
}

// 上一页 / 下一页 under a table, only when there is more than one page.
export function Pager({ page, total, size, onPage, children }: {
  page: number; total: number; size: number; onPage: (page: number) => void; children: ReactNode;
}) {
  const pages = Math.max(1, Math.ceil(total / size));
  // The total can shrink under the viewer (a refetch, a removed library): never stay on a page past the last one.
  useEffect(() => { if (page > pages) onPage(pages); }, [page, pages, onPage]);
  return (
    <div className="pager">
      {children}
      {(pages > 1 || page > 1) && (
        <span className="actions">
          <button type="button" className="btn small ghost" disabled={page <= 1} onClick={() => onPage(page - 1)}>上一页</button>
          <span className="mono">{page} / {pages}</span>
          <button type="button" className="btn small ghost" disabled={page >= pages} onClick={() => onPage(page + 1)}>下一页</button>
        </span>
      )}
    </div>
  );
}
