import type { ConnectionState } from "../stage/types";

export type SequenceResult =
  | { readonly kind: "accepted"; readonly cursor: number }
  | { readonly kind: "duplicate"; readonly cursor: number }
  | { readonly kind: "gap"; readonly cursor: number; readonly expected: number };

export class SequenceCursor {
  private _cursor: number;
  private _state: ConnectionState;

  public constructor(initial = 0) {
    if (!Number.isInteger(initial) || initial < 0) {
      throw new RangeError("sequence cursor must be a non-negative integer");
    }
    this._cursor = initial;
    this._state = "disconnected";
  }

  public get cursor(): number {
    return this._cursor;
  }

  public get state(): ConnectionState {
    return this._state;
  }

  public connecting(): void {
    this._state = "connecting";
  }

  public connected(): void {
    this._state = "connected";
  }

  public terminal(): void {
    this._state = "terminal";
  }

  public accept(sequence: number): SequenceResult {
    if (!Number.isInteger(sequence) || sequence < 1) {
      throw new RangeError("event sequence must be a positive integer");
    }
    if (sequence <= this._cursor) {
      return { kind: "duplicate", cursor: this._cursor };
    }
    if (sequence !== this._cursor + 1) {
      this._state = "catching-up";
      return { kind: "gap", cursor: this._cursor, expected: this._cursor + 1 };
    }
    this._cursor = sequence;
    if (this._state === "catching-up" || this._state === "connecting" || this._state === "disconnected") {
      this._state = "connected";
    }
    return { kind: "accepted", cursor: this._cursor };
  }
}
