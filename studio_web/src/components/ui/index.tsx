// Adapted from shadcn/ui (MIT), new-york-v4 registry.
// Styling uses Studio's CSS tokens instead of Tailwind utilities. See README.md.
import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import * as TabsPrimitive from '@radix-ui/react-tabs';
import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';
import { clsx } from 'clsx';
import { X } from 'lucide-react';

const buttonVariants = cva('ui-button', {
  variants: {
    variant: { default: 'primary', outline: 'ui-outline', secondary: 'ui-secondary', ghost: 'ui-ghost', destructive: 'danger' },
    size: { default: '', sm: 'ui-small', icon: 'ui-icon' },
  },
  defaultVariants: { variant: 'outline', size: 'default' },
});
export function Button({ className, variant, size, asChild = false, type = 'button', ...props }: React.ComponentProps<'button'> & VariantProps<typeof buttonVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot : 'button';
  return <Comp data-slot="button" type={type} className={clsx(buttonVariants({ variant, size }), className)} {...props} />;
}
export function Card({ className, ...props }: React.ComponentProps<'div'>) {
  return <div data-slot="card" className={clsx('panel', className)} {...props} />;
}
export function Badge({ className, ...props }: React.ComponentProps<'span'>) {
  return <span data-slot="badge" className={clsx('ui-badge', className)} {...props} />;
}
export function Input({ className, ...props }: React.ComponentProps<'input'>) {
  return <input data-slot="input" className={clsx('ui-input', className)} {...props} />;
}
export function Textarea({ className, ...props }: React.ComponentProps<'textarea'>) {
  return <textarea data-slot="textarea" className={clsx('ui-input', className)} {...props} />;
}
export function NativeSelect({ className, ...props }: React.ComponentProps<'select'>) {
  return <select data-slot="native-select" className={clsx('ui-input', className)} {...props} />;
}
export const Tabs = TabsPrimitive.Root;
export function TabsList({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.List>) {
  return <TabsPrimitive.List data-slot="tabs-list" className={clsx('tabs', className)} {...props} />;
}
export function TabsTrigger({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return <TabsPrimitive.Trigger data-slot="tabs-trigger" className={clsx('tabs-trigger', className)} {...props} />;
}
export function TabsContent({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return <TabsPrimitive.Content data-slot="tabs-content" className={clsx('tab-content', className)} {...props} />;
}
export function Dialog({ title, description, className, children, onClose }: { title: string; description?: string; className?: string; children: React.ReactNode; onClose: () => void }) {
  // These dialogs can also open from another dialog. Capture the invoking control
  // so closing restores focus even though their triggers live outside the root.
  const returnFocus = React.useRef(document.activeElement as HTMLElement | null);
  return <DialogPrimitive.Root open onOpenChange={open => { if (!open) onClose(); }}>
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="modal-backdrop" />
      <DialogPrimitive.Content className={clsx('modal', className)} {...(!description ? { 'aria-describedby': undefined } : {})}
        onCloseAutoFocus={event => { event.preventDefault(); if (returnFocus.current?.isConnected) returnFocus.current.focus(); }}>
        <div className="modal-heading">
          <div><span className="eyebrow">ANYBENCH STUDIO</span><DialogPrimitive.Title>{title}</DialogPrimitive.Title></div>
          <DialogPrimitive.Close asChild><Button size="icon" variant="ghost" aria-label="Close"><X size={18} /></Button></DialogPrimitive.Close>
        </div>
        {description && <DialogPrimitive.Description>{description}</DialogPrimitive.Description>}
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  </DialogPrimitive.Root>;
}
