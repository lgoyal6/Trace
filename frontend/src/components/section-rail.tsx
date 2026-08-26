// frontend/src/components/section-rail.tsx
// Shared "§NN - hairline - EYEBROW" motif that opens every section:
// design-system/MASTER.md section 4b "Section rail".
type Props = {
  number: string;
  eyebrow: string;
  className?: string;
};

export function SectionRail({ number, eyebrow, className }: Props) {
  return (
    <div className={`flex items-center gap-3 ${className ?? ""}`}>
      <span className="font-data text-xs text-blue-500">{number}</span>
      <span className="h-px w-7 bg-blue-300" aria-hidden="true" />
      <span className="eyebrow">{eyebrow}</span>
    </div>
  );
}
