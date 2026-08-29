import { ReactNode } from "react";

type Props = {
  title?: string;
  children: ReactNode;
};

export function TechnicalDetails({ title = "Technical details", children }: Props) {
  return (
    <details className="technical-details">
      <summary>{title}</summary>
      <div className="technical-details-body">{children}</div>
    </details>
  );
}
