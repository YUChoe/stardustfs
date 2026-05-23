"""
StardustFS 테스트 케이스
Windows에서 FUSE 없이 실행 가능
"""
import os
import sys
import stat

# 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.mock_fuse import MockFUSE, TestEnvironment


def run_tests():
    """모든 테스트 실행"""
    print("=" * 60)
    print("StardustFS Mock 테스트")
    print("=" * 60)

    with TestEnvironment(node_count=3) as config:
        print(f"\n테스트 환경:")
        print(f"  마운트 포인트: {config['abs_mount_point']}")
        for i, node in enumerate(config['nodes']):
            print(f"  노드 {i+1}: {node['abs_path']}")

        # stardustlib 설정 주입
        import stardustlib
        stardustlib.config.clear()
        stardustlib.config.update(config)

        # StardustOperations 인스턴스 생성 (FUSE 의존성 없음)
        ops = stardustlib.StardustOperations(config)

        mock = MockFUSE(ops)

        passed = 0
        failed = 0

        # 테스트 1: 루트 디렉토리 조회
        print("\n[테스트 1] 루트 디렉토리 조회")
        try:
            result = mock.ls('/')
            assert '.' in result and '..' in result
            print(f"  ✓ 통과: {result}")
            passed += 1
        except Exception as e:
            print(f"  ✗ 실패: {e}")
            failed += 1

        # 테스트 2: 디렉토리 생성
        print("\n[테스트 2] 디렉토리 생성")
        try:
            mock.mkdir('/testdir', 0o755)
            result = mock.ls('/')
            assert 'testdir' in result
            print(f"  ✓ 통과: testdir 생성됨")
            passed += 1
        except Exception as e:
            print(f"  ✗ 실패: {e}")
            failed += 1

        # 테스트 3: 모든 노드에 디렉토리 복제 확인
        print("\n[테스트 3] 모든 노드에 디렉토리 복제 확인")
        try:
            all_exist = True
            for node in config['nodes']:
                path = os.path.join(node['abs_path'], 'testdir')
                if not os.path.isdir(path):
                    all_exist = False
                    print(f"  - {path} 없음")
            assert all_exist
            print(f"  ✓ 통과: 모든 노드에 복제됨")
            passed += 1
        except Exception as e:
            print(f"  ✗ 실패: {e}")
            failed += 1

        # 테스트 4: 파일 생성 및 읽기
        print("\n[테스트 4] 파일 생성 및 읽기")
        try:
            test_content = b"Hello StardustFS!"
            mock.write_file('/testdir/hello.txt', test_content)
            read_content = mock.cat('/testdir/hello.txt')
            assert read_content == test_content
            print(f"  ✓ 통과: 파일 내용 일치")
            passed += 1
        except Exception as e:
            print(f"  ✗ 실패: {e}")
            failed += 1

        # 테스트 5: 파일 속성 조회
        print("\n[테스트 5] 파일 속성 조회")
        try:
            st = mock.stat('/testdir/hello.txt')
            assert 'st_size' in st
            assert st['st_size'] == len(test_content)
            print(f"  ✓ 통과: 크기={st['st_size']}")
            passed += 1
        except Exception as e:
            print(f"  ✗ 실패: {e}")
            failed += 1

        # 테스트 6: 파일 삭제
        print("\n[테스트 6] 파일 삭제")
        try:
            mock.rm('/testdir/hello.txt')
            assert not mock.exists('/testdir/hello.txt')
            print(f"  ✓ 통과: 파일 삭제됨")
            passed += 1
        except Exception as e:
            print(f"  ✗ 실패: {e}")
            failed += 1

        # 테스트 7: Round-Robin 노드 분배
        print("\n[테스트 7] Round-Robin 노드 분배")
        try:
            files_per_node = {node['abs_path']: [] for node in config['nodes']}

            for i in range(6):
                filename = f'/testdir/file{i}.txt'
                mock.write_file(filename, f"content{i}".encode())

            for node in config['nodes']:
                node_path = os.path.join(node['abs_path'], 'testdir')
                if os.path.isdir(node_path):
                    files = [f for f in os.listdir(node_path) if f.startswith('file')]
                    files_per_node[node['abs_path']] = files

            print(f"  노드별 파일 분포:")
            for path, files in files_per_node.items():
                print(f"    {os.path.basename(path)}: {files}")

            # 최소 2개 노드에 파일이 분배되었는지 확인
            nodes_with_files = sum(1 for files in files_per_node.values() if files)
            assert nodes_with_files >= 2
            print(f"  ✓ 통과: {nodes_with_files}개 노드에 분배됨")
            passed += 1
        except Exception as e:
            print(f"  ✗ 실패: {e}")
            failed += 1

        # 결과 요약
        print("\n" + "=" * 60)
        print(f"테스트 결과: {passed} 통과, {failed} 실패")
        print("=" * 60)

        return failed == 0


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
