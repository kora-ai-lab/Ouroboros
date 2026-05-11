import { useState, useCallback, useEffect } from "react";

interface BubblePosition {
  x: number;
  y: number;
}

const BUBBLE_SIZE = 44;

export function useBubble() {
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      setDragging(true);
      setDragStart({
        x: e.clientX,
        y: e.clientY,
      });
    },
    [],
  );

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (!dragging) return;
      setDragOffset({
        x: Math.max(0, dragStart.x - e.clientX),
        y: Math.max(0, dragStart.y - e.clientY),
      });
    },
    [dragging, dragStart],
  );

  const onMouseUp = useCallback(() => setDragging(false), []);

  return { dragOffset, dragging, onMouseDown, onMouseMove, onMouseUp };
}