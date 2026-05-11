import React, { useState, useMemo, useEffect, useRef, useCallback, Fragment } from 'react';
import { antdProxy as antd } from '@/utils/antdProxy';
const { Button, Pagination, List, Dropdown, Modal, Popconfirm } = antd;
import { PlusOutlined } from '@ant-design/icons';
import classNames from 'classnames';
import EllipsisText from '@/component/EllipsisText';
import { useRequest } from '@/hooks/useRequest';
import EditForm, { FormItemConfig } from '@/component/EditForm';
import styles from './index.module.less';
import { nanoid } from 'nanoid';
import request from 'utils/request';
import { getStrByParams } from '@/utils/dataTransfer';

interface ConversationListProps {
  userName: string;
  className?: string;
  onSelect?: (id: string) => void;
}

const ConversationList: React.FC<ConversationListProps> = ({ userName, onSelect, className }) => {
  const [selectedKeys, setSelectedKeys] = useState<any[]>([]);
  const [currentPage, setCurrentPage] = useState<DataTable.CurrentPage>({
    pageSize: 1000,
    current: 1,
  });

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
    data: tableData,
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
    onSuccess: data => selectedKeys.length <= 0 && setSelectedKeys([data[0].sessionId]),
  });
  const handleCancelEdit = () => {
    setOpenEdit(false);
    setCurrentRecord(null);
  };

  const { run: addConv, loading: loadingAddConv } = useRequest<any>(
    (record: any, initialValues: any) => {
      // console.log(record, initialValues);
      if (initialValues?.id) {
        return request({
          url: '/aiSessionLog/aiSessionTopic/edit',
          method: 'PUT',
          params: { ...initialValues, ...record, userName },
        });
      } else {
        return request({
          url: '/aiSessionLog/aiSessionTopic/add',
          method: 'POST',
          params: { ...record, sessionId: nanoid(), userName },
        });
      }
    },
    {
      onSuccess: () => {
        handleCancelEdit();
        getPageData({ ...currentPage, userName });
      },
    },
  );

  const { run: deleteConv, loading: loadingdeleteConv } = useRequest<any>(
    (id: string) => {
      return request({
        url: `/aiSessionLog/aiSessionTopic/delete${getStrByParams({ id })}`,
        method: 'DELETE',
      });
    },
    {
      onSuccess: () => {
        setCurrentPage({ pageSize: 1000, current: 1 });
      },
    },
  );
  const listItemClick = (item: any) => {
    setSelectedKeys([item.sessionId]);
  };
  useEffect(() => {
    getPageData({ ...currentPage, userName });
  }, [currentPage, userName, ]);

  useEffect(() => {
    selectedKeys[0] && onSelect?.(selectedKeys[0]);
  }, [selectedKeys]);

  const handleDeleteRecord = () => {};
  const dropdownMenus = [
    { key: '1', label: <Button type="text">编辑话题</Button> },
    {
      key: '2',
      label: (
        <Button type="text" danger>
          删除话题
        </Button>
      ),
    },
  ];
  const clickDropdownItem = (menuItem: any, item: any) => {
    switch (menuItem.key) {
      case '1':
        setOpenEdit(true);
        setEditTitle('编辑话题名');
        setCurrentRecord(item);
        break;
      case '2':
        deleteConv(item?.id);
        break;
      default:
        break;
    }
  };
  const [openEdit, setOpenEdit] = useState<boolean>(false);
  const [editTitle, setEditTitle] = useState<string>('');
  const [currentRecord, setCurrentRecord] = useState<any>(null);
  const formConfigs: FormItemConfig[] = [
    {
      label: '名称',
      name: 'sessionName',
      rules: [{ required: true, message: '请输入话题名称' }],
    },
  ];
  const handleAddConversation = () => {
    setOpenEdit(true);
    setEditTitle('新建话题名');
    setCurrentRecord({ sessionName: '' });
  };
  return (
    <div className={`flex flex-col h-full overflow-hidden ${styles.conversationList} ${className}`}>
      <div className="cursor-pointer gap-2 flex items-center justify-center text-white text-base btn-add-item" onClick={handleAddConversation}>
        <PlusOutlined />
        新建话题
      </div>
      <List
        dataSource={tableData}
        loading={loading}
        bordered={false}
        className={classNames('flex-1', 'overflow-y-auto')}
        renderItem={(item: any, index) => (
          <Dropdown
            trigger={['contextMenu']}
            menu={{
              items: dropdownMenus,
              onClick: menuItem => clickDropdownItem(menuItem, item),
            }}
          >
            <List.Item
              onClick={() => listItemClick(item)}
              className={classNames('cursor-pointer', 'space-x-[1rem]', 'mb-2', 'flex', 'items-center','justify-center', {
                isActive: (selectedKeys || []).includes(item.sessionId),
              })}
            >
              <EllipsisText rows={2} expandable={false}>
                {item.sessionName}
              </EllipsisText>
            </List.Item>
          </Dropdown>
        )}
      ></List>

      <Modal
        open={openEdit}
        title={editTitle}
        footer={null}
        destroyOnClose={true}
        width={600}
        loading={loadingAddConv}
        onCancel={handleCancelEdit}
      >
        <EditForm
          onCancel={handleCancelEdit}
          onSubmit={addConv}
          initialValues={currentRecord}
          layout="horizontal"
          formConfigs={formConfigs}
        />
      </Modal>
    </div>
  );
};

export default ConversationList;
