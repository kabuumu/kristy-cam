import { useState } from "react";
import {
  AlertCircle,
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  BookOpen,
  CheckCircle2,
  Merge,
  Mic,
  Plus,
  Scissors,
  Trash2,
  Volume2,
} from "lucide-react";
import { apiClient } from "../api/client";
import type {
  ArtifactManifest,
  ClauseInput,
  DurationMode,
  ProjectInput,
  SegmentInput,
} from "../types/studio";
import {
  autoSplitTextToClauses,
  computeClientTtsSignature,
  resolveClauseSpokenText,
} from "../utils/studioMath";

interface ScriptPanelProps {
  project: ProjectInput;
  manifest: ArtifactManifest;
  selectedSegment: SegmentInput | null;
  voices: string[];
  ttsError: string | null;
  lastSynthesizedSignatures: Record<string, string>;
  onUpdateProject: (updater: (prev: ProjectInput) => ProjectInput) => void;
  onUpdateSegment: (updated: SegmentInput) => void;
}

export function ScriptPanel({
  project,
  manifest,
  selectedSegment,
  voices,
  ttsError,
  lastSynthesizedSignatures,
  onUpdateProject,
  onUpdateSegment,
}: ScriptPanelProps) {
  const [newPronKey, setNewPronKey] = useState("");
  const [newPronVal, setNewPronVal] = useState("");
  const [showPronEditor, setShowPronEditor] = useState(true);

  const voiceOptions = voices.length > 0
    ? voices.includes(project.voice)
      ? voices
      : [project.voice, ...voices]
    : [project.voice];

  const segArtifacts = selectedSegment
    ? manifest.segments[selectedSegment.id]
    : undefined;

  const currentSig = selectedSegment
    ? computeClientTtsSignature(
        selectedSegment,
        project.pronunciations,
        project.voice,
        project.language,
        project.output_settings.inter_clause_pause_ms
      )
    : "";

  const hasClauses = Boolean(
    selectedSegment &&
      selectedSegment.clauses.some((c) => c.display_text.trim().length > 0)
  );
  const lastSig = selectedSegment
    ? lastSynthesizedSignatures[selectedSegment.id]
    : undefined;
  const hasAudioArtifact = Boolean(segArtifacts?.audio_artifact_id);
  const isStale =
    Boolean(selectedSegment) &&
    hasClauses &&
    (!hasAudioArtifact || (lastSig !== undefined && lastSig !== currentSig));

  const nextClauseId = (clauses: ClauseInput[]): string => {
    let idx = clauses.length + 1;
    const existing = new Set(clauses.map((c) => c.id));
    while (existing.has(`c${idx}`)) idx += 1;
    return `c${idx}`;
  };

  const handleAddClause = () => {
    if (!selectedSegment) return;
    const id = nextClauseId(selectedSegment.clauses);
    onUpdateSegment({
      ...selectedSegment,
      clauses: [
        ...selectedSegment.clauses,
        { id, display_text: "", spoken_text_override: null },
      ],
    });
  };

  const handleUpdateClause = (
    idx: number,
    patch: Partial<ClauseInput>
  ) => {
    if (!selectedSegment) return;
    const next = selectedSegment.clauses.map((c, i) =>
      i === idx ? { ...c, ...patch } : c
    );
    onUpdateSegment({ ...selectedSegment, clauses: next });
  };

  const handleDeleteClause = (idx: number) => {
    if (!selectedSegment) return;
    onUpdateSegment({
      ...selectedSegment,
      clauses: selectedSegment.clauses.filter((_, i) => i !== idx),
    });
  };

  const handleMoveClause = (idx: number, dir: -1 | 1) => {
    if (!selectedSegment) return;
    const target = idx + dir;
    if (target < 0 || target >= selectedSegment.clauses.length) return;
    const next = [...selectedSegment.clauses];
    [next[idx], next[target]] = [next[target], next[idx]];
    onUpdateSegment({ ...selectedSegment, clauses: next });
  };

  const handleSplitClause = (idx: number) => {
    if (!selectedSegment) return;
    const clause = selectedSegment.clauses[idx];
    const parts = autoSplitTextToClauses(clause.display_text);
    if (parts.length <= 1) {
      // Split at midpoint word boundary if single chunk
      const words = clause.display_text.trim().split(/\s+/);
      if (words.length < 2) return;
      const mid = Math.ceil(words.length / 2);
      parts.splice(0, 1, words.slice(0, mid).join(" "), words.slice(mid).join(" "));
    }
    const existingIds = new Set(selectedSegment.clauses.map((c) => c.id));
    const newClauses: ClauseInput[] = parts.map((text, pIdx) => {
      if (pIdx === 0) {
        return {
          id: clause.id,
          display_text: text,
          spoken_text_override: null,
        };
      }
      let counter = selectedSegment.clauses.length + pIdx;
      while (existingIds.has(`c${counter}`)) counter += 1;
      existingIds.add(`c${counter}`);
      return {
        id: `c${counter}`,
        display_text: text,
        spoken_text_override: null,
      };
    });
    const next = [
      ...selectedSegment.clauses.slice(0, idx),
      ...newClauses,
      ...selectedSegment.clauses.slice(idx + 1),
    ];
    onUpdateSegment({ ...selectedSegment, clauses: next });
  };

  const handleMergeWithNext = (idx: number) => {
    if (!selectedSegment || idx >= selectedSegment.clauses.length - 1) return;
    const curr = selectedSegment.clauses[idx];
    const nextClause = selectedSegment.clauses[idx + 1];
    const merged: ClauseInput = {
      id: curr.id,
      display_text: `${curr.display_text.trim()} ${nextClause.display_text.trim()}`.trim(),
      spoken_text_override: null,
    };
    const next = [
      ...selectedSegment.clauses.slice(0, idx),
      merged,
      ...selectedSegment.clauses.slice(idx + 2),
    ];
    onUpdateSegment({ ...selectedSegment, clauses: next });
  };

  const handleAddPronunciation = () => {
    const k = newPronKey.trim();
    const v = newPronVal.trim();
    if (!k || !v) return;
    onUpdateProject((prev) => ({
      ...prev,
      pronunciations: { ...prev.pronunciations, [k]: v },
    }));
    setNewPronKey("");
    setNewPronVal("");
  };

  const handleDeletePronunciation = (key: string) => {
    onUpdateProject((prev) => {
      const next = { ...prev.pronunciations };
      delete next[key];
      return { ...prev, pronunciations: next };
    });
  };

  return (
    <aside className="flex w-full lg:w-[430px] shrink-0 flex-col overflow-hidden rounded-xl border border-zinc-800 bg-[#151821]">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <div className="flex items-center gap-2">
          <Mic className="h-4 w-4 text-emerald-400" />
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-200">
            Narration &amp; Pronunciations
          </h2>
        </div>

        {/* Project Voice Selector */}
        <label className="flex items-center gap-1.5 text-xs text-zinc-300">
          <span>Voice</span>
          <select
            aria-label="Project Voice"
            value={project.voice}
            onChange={(e) =>
              onUpdateProject((prev) => ({ ...prev, voice: e.target.value }))
            }
            className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100 focus:border-emerald-500 focus:outline-none"
          >
            {voiceOptions.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-4">
        {!selectedSegment ? (
          <div className="rounded-lg border border-dashed border-zinc-700 p-6 text-center text-xs text-zinc-400">
            Select or add a segment on the timeline to edit narration clauses.
          </div>
        ) : (
          <>
            {/* Segment Audio & Timing Controls */}
            <div className="grid grid-cols-2 gap-3 rounded-lg border border-zinc-800 bg-[#11141b] p-2.5 text-xs">
              <label className="flex flex-col gap-1">
                <span className="text-zinc-400">Duration Mode</span>
                <select
                  aria-label="Duration Mode"
                  value={selectedSegment.duration_mode}
                  onChange={(e) =>
                    onUpdateSegment({
                      ...selectedSegment,
                      duration_mode: e.target.value as DurationMode,
                    })
                  }
                  className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-100 focus:border-emerald-500 focus:outline-none"
                >
                  <option value="preserve_video">Preserve Video</option>
                  <option value="fit_narration">Fit Narration</option>
                </select>
              </label>

              <label className="flex flex-col gap-1">
                <div className="flex justify-between text-zinc-400">
                  <span>Speed</span>
                  <span className="font-mono text-zinc-200">
                    {selectedSegment.speed.toFixed(2)}x
                  </span>
                </div>
                <input
                  type="range"
                  aria-label="Narration Speed"
                  min={0.8}
                  max={1.25}
                  step={0.05}
                  value={selectedSegment.speed}
                  onChange={(e) =>
                    onUpdateSegment({
                      ...selectedSegment,
                      speed: parseFloat(e.target.value),
                    })
                  }
                />
              </label>

              <label className="col-span-2 flex flex-col gap-1">
                <div className="flex justify-between text-zinc-400">
                  <span className="inline-flex items-center gap-1">
                    <Volume2 className="h-3.5 w-3.5" />
                    Source Game Audio Volume
                  </span>
                  <span className="font-mono text-zinc-200">
                    {Math.round(selectedSegment.source_audio_volume * 100)}%
                  </span>
                </div>
                <input
                  type="range"
                  aria-label="Source Audio Volume"
                  min={0}
                  max={1}
                  step={0.05}
                  value={selectedSegment.source_audio_volume}
                  onChange={(e) =>
                    onUpdateSegment({
                      ...selectedSegment,
                      source_audio_volume: parseFloat(e.target.value),
                    })
                  }
                />
              </label>
            </div>

            {/* Voiceover Status & Audio Player (synthesized automatically on Render Segment Preview / Export) */}
            <div className="space-y-2 rounded-lg border border-zinc-800 bg-[#11141b] p-2.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-[11px] font-medium text-zinc-400">
                  Voiceover Status
                </span>

                <div className="flex items-center gap-1.5">
                  {ttsError && (
                    <span className="inline-flex items-center gap-1 rounded border border-rose-500/50 bg-rose-500/20 px-2 py-0.5 text-[11px] font-medium text-rose-200">
                      <AlertCircle className="h-3 w-3" />
                      Error
                    </span>
                  )}

                  {isStale ? (
                    <span className="inline-flex items-center gap-1 rounded border border-amber-500/50 bg-amber-500/15 px-2 py-0.5 text-[11px] font-medium text-amber-200">
                      <AlertTriangle className="h-3 w-3" />
                      Narration Updated — Click Preview to Sync
                    </span>
                  ) : hasAudioArtifact ? (
                    <span className="inline-flex items-center gap-1 rounded border border-emerald-500/40 bg-emerald-500/15 px-2 py-0.5 text-[11px] font-medium text-emerald-200">
                      <CheckCircle2 className="h-3 w-3" />
                      Voiceover Ready ({segArtifacts?.audio_duration.toFixed(2)}s)
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-0.5 text-[11px] text-zinc-400">
                      Silent / No Narration
                    </span>
                  )}
                </div>
              </div>

              {ttsError && (
                <p className="text-[11px] text-rose-300">{ttsError}</p>
              )}

              {segArtifacts?.audio_artifact_id && (
                <audio
                  controls
                  src={`${apiClient.mediaUrl(project.project_id, segArtifacts.audio_artifact_id)}?v=${segArtifacts.tts_hash ?? ""}`}
                  className="h-8 w-full"
                />
              )}
            </div>

            {/* Paired Clause List Editor */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-xs font-semibold text-zinc-200">
                  Paired Caption &amp; Narration Clauses ({selectedSegment.clauses.length})
                </h3>
                <button
                  type="button"
                  onClick={handleAddClause}
                  className="inline-flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-[11px] text-zinc-200 hover:bg-zinc-700"
                >
                  <Plus className="h-3 w-3" />
                  Add Clause
                </button>
              </div>

              {selectedSegment.clauses.length === 0 ? (
                <p className="rounded border border-dashed border-zinc-800 p-3 text-center text-[11px] text-zinc-500">
                  Empty clause list = silent gameplay segment (no TTS or captions).
                </p>
              ) : (
                <div className="space-y-2.5">
                  {selectedSegment.clauses.map((clause, idx) => {
                    const resolvedSpoken = resolveClauseSpokenText(
                      clause,
                      project.pronunciations
                    );
                    const wordCount = clause.display_text
                      .trim()
                      .split(/\s+/)
                      .filter(Boolean).length;

                    return (
                      <div
                        key={clause.id}
                        className="rounded-lg border border-zinc-800 bg-[#11141b] p-2.5 space-y-2"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-mono text-[11px] text-zinc-400">
                            {clause.id} • {wordCount}w
                          </span>
                          <div className="flex items-center gap-1">
                            <button
                              type="button"
                              onClick={() => handleMoveClause(idx, -1)}
                              disabled={idx === 0}
                              className="rounded p-1 text-zinc-400 hover:bg-zinc-800 disabled:opacity-30"
                              title="Move clause up"
                            >
                              <ArrowUp className="h-3 w-3" />
                            </button>
                            <button
                              type="button"
                              onClick={() => handleMoveClause(idx, 1)}
                              disabled={idx === selectedSegment.clauses.length - 1}
                              className="rounded p-1 text-zinc-400 hover:bg-zinc-800 disabled:opacity-30"
                              title="Move clause down"
                            >
                              <ArrowDown className="h-3 w-3" />
                            </button>
                            <button
                              type="button"
                              onClick={() => handleSplitClause(idx)}
                              className="inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] text-zinc-300 hover:bg-zinc-800"
                              title="Split clause (3–6 word guideline)"
                            >
                              <Scissors className="h-3 w-3" />
                              Split
                            </button>
                            {idx < selectedSegment.clauses.length - 1 && (
                              <button
                                type="button"
                                onClick={() => handleMergeWithNext(idx)}
                                className="inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] text-zinc-300 hover:bg-zinc-800"
                                title="Merge with next clause"
                              >
                                <Merge className="h-3 w-3" />
                                Merge
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() => handleDeleteClause(idx)}
                              className="rounded p-1 text-zinc-400 hover:bg-rose-500/20 hover:text-rose-300"
                              title="Delete clause"
                            >
                              <Trash2 className="h-3 w-3" />
                            </button>
                          </div>
                        </div>

                        <label className="block space-y-1">
                          <span className="text-[11px] text-zinc-400">
                            Caption Display Text
                          </span>
                          <input
                            type="text"
                            value={clause.display_text}
                            onChange={(e) =>
                              handleUpdateClause(idx, {
                                display_text: e.target.value,
                              })
                            }
                            placeholder="Exact on-screen caption..."
                            className="w-full rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100 focus:border-emerald-500 focus:outline-none"
                          />
                        </label>

                        {/* Render exact display_text as DOM text for accessibility & live caption preview */}
                        <div className="rounded bg-zinc-900/70 px-2 py-1 text-[11px] text-zinc-300">
                          <span className="text-zinc-500">Caption: </span>
                          <span>{clause.display_text || "—"}</span>
                        </div>

                        <label className="block space-y-1">
                          <span className="text-[11px] text-zinc-400">
                            Spoken Override (optional)
                          </span>
                          <input
                            type="text"
                            value={clause.spoken_text_override ?? ""}
                            onChange={(e) =>
                              handleUpdateClause(idx, {
                                spoken_text_override:
                                  e.target.value.length > 0
                                    ? e.target.value
                                    : null,
                              })
                            }
                            placeholder="Leave blank to use dictionary..."
                            className="w-full rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100 focus:border-emerald-500 focus:outline-none"
                          />
                        </label>

                        <div className="rounded bg-emerald-950/30 px-2 py-1 text-[11px] text-emerald-200">
                          <span className="text-emerald-400">Spoken preview: </span>
                          <span>{resolvedSpoken || "—"}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </>
        )}

        {/* Inline Project Pronunciation Dictionary Editor */}
        <div className="rounded-lg border border-zinc-800 bg-[#11141b] p-2.5 space-y-2">
          <button
            type="button"
            onClick={() => setShowPronEditor((v) => !v)}
            className="flex w-full items-center justify-between text-xs font-semibold text-zinc-200"
          >
            <span className="inline-flex items-center gap-1.5">
              <BookOpen className="h-3.5 w-3.5 text-emerald-400" />
              Project Pronunciations ({Object.keys(project.pronunciations).length})
            </span>
            <span className="text-[11px] text-zinc-400">
              {showPronEditor ? "Hide" : "Show"}
            </span>
          </button>

          {showPronEditor && (
            <div className="space-y-2 pt-1">
              <p className="text-[10px] text-zinc-500">
                Plain-language respellings are phonetic hints to Kokoro (longest key first, word-boundary).
              </p>

              <div className="flex gap-1.5">
                <input
                  type="text"
                  value={newPronKey}
                  onChange={(e) => setNewPronKey(e.target.value)}
                  placeholder="Word (e.g. Vaelen)"
                  className="w-1/2 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100 focus:border-emerald-500 focus:outline-none"
                />
                <input
                  type="text"
                  value={newPronVal}
                  onChange={(e) => setNewPronVal(e.target.value)}
                  placeholder="Spoken (e.g. Vay-len)"
                  className="w-1/2 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100 focus:border-emerald-500 focus:outline-none"
                />
                <button
                  type="button"
                  onClick={handleAddPronunciation}
                  className="rounded bg-zinc-800 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-700"
                  title="Add pronunciation"
                >
                  <Plus className="h-3.5 w-3.5" />
                </button>
              </div>

              <div className="max-h-36 space-y-1 overflow-y-auto">
                {Object.entries(project.pronunciations).map(([k, v]) => (
                  <div
                    key={k}
                    className="flex items-center gap-1.5 rounded bg-zinc-900/80 px-2 py-1 text-xs"
                  >
                    <span className="w-28 truncate font-medium text-zinc-200">
                      {k}
                    </span>
                    <span className="text-zinc-500">→</span>
                    <input
                      type="text"
                      value={v}
                      onChange={(e) =>
                        onUpdateProject((prev) => ({
                          ...prev,
                          pronunciations: {
                            ...prev.pronunciations,
                            [k]: e.target.value,
                          },
                        }))
                      }
                      className="min-w-0 flex-1 rounded border border-zinc-800 bg-zinc-950 px-1.5 py-0.5 text-xs text-emerald-200 focus:border-emerald-500 focus:outline-none"
                    />
                    <button
                      type="button"
                      onClick={() => handleDeletePronunciation(k)}
                      className="rounded p-0.5 text-zinc-500 hover:text-rose-400"
                      title={`Remove ${k}`}
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}
