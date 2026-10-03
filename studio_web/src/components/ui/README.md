# Studio primitives

Button, Card, Badge, Input, Native Select, Textarea, Dialog and Tabs are adapted from [shadcn/ui's MIT-licensed new-york-v4 registry](https://github.com/shadcn-ui/ui/tree/main/apps/v4/registry/new-york-v4/ui). The upstream license is in `LICENSE.shadcn`.

Studio uses named CSS classes and the shadcn semantic color tokens in `studio.css` rather than adding Tailwind. Dialog and Tabs retain Radix's focus management, Escape dismissal, arrow-key navigation, and ARIA semantics. Button retains Slot composition and class-variance-authority variants. The application-specific Dialog wrapper provides its title and close control consistently.
