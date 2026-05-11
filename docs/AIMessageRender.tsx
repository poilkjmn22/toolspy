import React, { useState, useCallback, useEffect } from 'react';
import type { Message } from './type';
import MarkdownViewer from '@/component/MarkdownViewer';
import { DownOutlined, RightOutlined } from '@ant-design/icons';
import { Loader2, Brain } from 'lucide-react';
import { getReadableTimeInterval } from '@/utils/timeTools';
import DocLoops from './DocLoops';

interface AIMessageRenderProps {
  message: Omit<Message, 'content'>;
  onViewLoop?: () => void;
}
let timer: any = null;
const AIMessageRender: React.FC<AIMessageRenderProps> = ({ message, onViewLoop }) => {
  const { think, answer, isThinking, startTime, endTime, contentType } = message;
  const [showThink, setShowThink] = useState(!!think || false);
  const [now, setNow] = useState(new Date());

  useEffect(() => {
    clearInterval(timer);
    if (startTime && !endTime) {
      timer = setInterval(() => {
        setNow(new Date());
      }, 1000);
    }
    return () => clearInterval(timer);
  }, [startTime, endTime]);
  useEffect(() => {
    if (isThinking === false) {
      clearInterval(timer);
      setNow(new Date());
    }
  }, [isThinking]);

  const renderContent = useCallback(() => {
    const { contentType, answer } = message;
    switch (contentType) {
      case 'loopInfo':
        return <DocLoops loopData={answer} />;
      default:
        return <MarkdownViewer onViewLoop={onViewLoop} className="" content={answer} />;
    }
  }, [message, onViewLoop]);
  return (
    <div className="result flex flex-col space-y-[0.5rem]">
      {contentType !== 'loopInfo' && <div
        className="t-header cursor-pointer flex justify-between border-b border-[#1a2f45]"
        onClick={() => setShowThink(!showThink)}
      >
        <div className="flex items-center gap-2 text-gray-400">
          <Brain className={`h-4 w-4 text-[#00d4ff] ${isThinking ? 'animate-pulse' : ''}`} />
          <span className="text-sm">{isThinking ? '正在思考...' : '已深度思考'}</span>
          <span className="text-sm">
            （用时
            {getReadableTimeInterval(
              startTime ? new Date(startTime.valueOf()) : undefined,
              endTime ? new Date(endTime.valueOf()) : now,
            )}
            ）
          </span>
        </div>
        {showThink ? <DownOutlined /> : <RightOutlined />}
      </div>}
      {think && (
        <div className={`t-body whitespace-pre-wrap ${showThink ? '' : 'hidden'}`}>{think}</div>
      )}
      {answer && renderContent()}
    </div>
  );
};

export default AIMessageRender;
