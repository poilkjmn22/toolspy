import { useState, useRef, useEffect } from 'react';
import { Button } from './ui/button';
import { AIChatWindow } from './AIChatWindow';
import { useClickAway } from 'react-use';
import { useLocation } from 'react-router-dom';
import { BtnFloat } from '@/component/BtnCollection';
import { ConfigProvider } from 'antd';
import { ReactComponent as ChatSvg } from '@/common/images/chatAi/chat.svg';
import { ReactComponent as CollapseSvg } from '@/common/images/chatAi/collapse.svg';
import styles from './index.module.less';

export function AIChatButton() {
  const [isOpen, setIsOpen] = useState(false);
  const [enable, setEnable] = useState(false);
  const [position, setPosition] = useState({
    x: window.innerWidth - 80,
    y: window.innerHeight - 70,
  });
  const refChatWindow = useRef<HTMLDivElement>(null);
  useClickAway(refChatWindow, () => {
    // setIsOpen(false);
  });

  useEffect(() => {
    if (location.pathname.match(/\/SecondaryCircuit/)) {
      setEnable(true);
      setPosition({ x: window.innerWidth - 80, y: window.innerHeight - 120 });
    } else {
      setEnable(false);
      setIsOpen(false);
    }
  }, [location.pathname]);

  if (!enable) {
    return null;
  }

  return (
    <>
      {/* 浮动按钮 */}
      {!isOpen && (
        <BtnFloat
          position={position}
          tooltip="点击开始大模型对话"
          draggable={false}
          onClick={() => setIsOpen(true)}
        >
          <button
            className={`${styles.btnChat} h-14 w-14 rounded-full flex items-center justify-center`}
          >
            <ChatSvg className="h-7 w-7" />
          </button>
        </BtnFloat>
      )}

      {/* 对话窗口 */}
      {isOpen && (
        <div
          ref={refChatWindow}
          className={`fixed ${styles.chatWindow}`}
          style={{ zIndex: '900', right: '10px', bottom: '45px' }}
        >
          <ConfigProvider
            theme={{
              token: {
                colorPrimary: '#00d4ff',
                colorBgContainer: 'transparent',
                colorText: '#fff',
                colorTextPlaceholder: '#fff',
                fontFamily: 'Alibaba PuHuiTi 2.0',
                colorBgElevated: 'rgba(32, 47, 62, 0.6)',
                colorPrimaryBorder: 'rgba(64, 72, 106, 1)',
                colorTextDescription: '#C6D3EC',
                colorTextDisabled: '#C6D3EC',
                colorIcon: '#fff',
              },
              components: {
                Modal: {
                  headerBg: '#0d1b2a',
                  contentBg: '#0d1b2a',
                  titleColor: '#00d4ff',
                },
                Table: {
                  cellFontSize: 10,
                  cellPaddingInlineSM: 5,
                  cellPaddingBlockSM: 5,
                },
              },
            }}
          >
            <AIChatWindow onCloseWindow={() => setIsOpen(false)} />

            <button
              onClick={() => setIsOpen(false)}
              title="关闭对话窗口"
              className={`${styles.btnCollapse} h-4 w-6 cursor-pointer`}
            >
              <CollapseSvg className="w-full h-full" />
            </button>
          </ConfigProvider>
        </div>
      )}
    </>
  );
}
