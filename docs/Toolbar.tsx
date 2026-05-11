import React, {useState, useEffect, useCallback } from 'react';
import { ReactComponent as ChatSvg } from '@/common/images/chatAi/chat.svg';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
  DropdownMenuLabel,
} from './ui/dropdown-menu';
import { antdProxy as antd } from '@/utils/antdProxy';
const { Dropdown } = antd;
import { Button } from './ui/button';
import { Settings } from 'lucide-react';
import ConversationList from './ConversationList';
import AiChatConfig from './AiChatConfig';
import { useRequest } from '@/hooks/useRequest';
import { nanoid } from 'nanoid';
import request from 'utils/request';
import { getStrByParams } from '@/utils/dataTransfer';
import styles from './index.module.less';

interface ToolbarProps {
  userName: string;
  onSelectSession: (id: string) => void;
  onClickChat?: () => void;
}

const Toolbar: React.FC<ToolbarProps> = ({ userName, onSelectSession , onClickChat }) => {
  const _getPageData = useCallback(
    (values?: any) =>
      request({
        url: `/aiSessionLog/aiSessionTopic/list${getStrByParams({
          ...values,
        })}`,
        method: 'GET',
      }),
    [],
  );
  const {
    run: getPageData,
    loading,
  } = useRequest<any>(_getPageData, {
    initialData: [],
    formatResponse: res => {
      if (res?.result?.records?.length > 0) {
        return res.result.records;
      } else {
        const defaultSession = { sessionName: '默认话题', sessionId: nanoid(), userName };
        request({
          url: '/aiSessionLog/aiSessionTopic/add',
          method: 'POST',
          params: defaultSession,
        });
        return [defaultSession];
      }
    },
    onSuccess: data => onSelectSession(data[0].sessionId),
  });


  useEffect(() => {
    getPageData({ pageNo: 1, pageSize: 10000, userName });
  }, [ userName]);
  return (
    <div className={`${styles.toolbar} flex flex-col h-full overflow-hidden items-center justify-start p-2 gap-2`}>
      <ChatSvg onClick={() => onClickChat?.()} className="cursor-pointer" />
      <Dropdown trigger={['click']} placement="topLeft" menu={{items: [
        {key: 'conversationList', label: <ConversationList onSelect={onSelectSession} userName={userName} />},
      ]}} overlayClassName='ant-dropdown-menu-chatai conversationList'>
          <Button size="sm" variant="ghost" className="text-white text-base">
            <span>#</span>
          </Button>
      </Dropdown>
      <Dropdown trigger={['click']} placement="bottomLeft" menu={{items: [
        {key: 'aiChatConfig', label: <AiChatConfig className={styles.chatConfig} />},
      ]}} overlayClassName='ant-dropdown-menu-chatai config'>
          <Button size="sm" variant="ghost" className="text-white">
            <Settings />
          </Button>
      </Dropdown>
    </div>
  );
};

export default Toolbar;
