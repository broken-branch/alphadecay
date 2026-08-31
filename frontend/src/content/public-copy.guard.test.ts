/// <reference types="vite/client" />

import ts from "typescript";
import { describe, expect, it } from "vitest";

const componentSources = import.meta.glob<string>("../**/*.tsx", {
  eager: true,
  import: "default",
  query: "?raw",
});

const visibleAttributes = new Set(["alt", "aria-label", "placeholder", "title"]);

function findInlinePublicCopy(source: string, fileName: string): string[] {
  const sourceFile = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const findings: string[] = [];

  function visit(node: ts.Node) {
    if (ts.isJsxText(node) && node.text.trim()) {
      findings.push(node.text.trim());
    }

    if (
      ts.isJsxAttribute(node)
      && visibleAttributes.has(node.name.getText(sourceFile))
      && node.initializer
      && ts.isStringLiteral(node.initializer)
    ) {
      findings.push(node.initializer.text);
    }

    if (ts.isJsxExpression(node) && node.expression && ts.isStringLiteral(node.expression)) {
      findings.push(node.expression.text);
    }

    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
  return findings;
}

describe("public copy structure", () => {
  it("rejects JSX text and visible literal attributes", () => {
    expect(findInlinePublicCopy("<p>Inline words</p>", "sample.tsx")).toEqual(["Inline words"]);
    expect(findInlinePublicCopy("<button aria-label=\"Inline label\" />", "sample.tsx")).toEqual(["Inline label"]);
  });

  it("keeps authored component copy in the catalog", () => {
    const findings = Object.entries(componentSources)
      .filter(([path]) => !path.endsWith(".test.tsx"))
      .flatMap(([path, source]) => findInlinePublicCopy(source, path).map((text) => `${path}: ${text}`));

    expect(findings).toEqual([]);
  });
});
