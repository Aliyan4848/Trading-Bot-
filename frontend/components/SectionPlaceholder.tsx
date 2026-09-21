export function SectionPlaceholder({ title, note }: { title: string; note: string }) {
  return (
    <div className="rounded-lg border border-dashed border-neutral-800 bg-neutral-900/30 p-10 text-center">
      <h2 className="text-lg font-medium text-neutral-300">{title}</h2>
      <p className="mx-auto mt-2 max-w-md text-sm text-neutral-500">{note}</p>
    </div>
  );
}
